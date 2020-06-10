# Copyright (C) 2015-2019, Wazuh Inc.
# Created by Wazuh, Inc. <info@wazuh.com>.
# This program is free software; you can redistribute it and/or modify it under the terms of GPLv2

import asyncio
import itertools
import json
import logging
import operator
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from functools import reduce
from operator import or_
from typing import Callable, Dict, Tuple

import wazuh.core.cluster.cluster
import wazuh.core.cluster.utils
import wazuh.core.manager
import wazuh.results as wresults
from wazuh import exception, agent, common
from wazuh.core.cluster import local_client, common as c_common
from wazuh.exception import WazuhException, WazuhClusterError, WazuhError


class DistributedAPI:
    """
    Represents a distributed API request
    """

    def __init__(self, f: Callable, logger: logging.getLogger, f_kwargs: Dict = None, node: c_common.Handler = None,
                 debug: bool = False, request_type: str = "local_master",
                 wait_for_complete: bool = False, from_cluster: bool = False, is_async: bool = False,
                 broadcasting: bool = False, basic_services: tuple = None, local_client_arg: str = None,
                 rbac_permissions: Dict = None, nodes: list = None, current_user: str = ''):
        """
        Class constructor

        :param f: function to be executed
        :param f_kwargs: arguments to be passed to function `f`
        :param logger: Logging logger to use
        :param node: Asyncio protocol object to use when sending requests to other nodes
        :param debug: Enable debug messages and raise exceptions.
        :param wait_for_complete: true to disable timeout, false otherwise
        """
        self.logger = logger
        self.f = f
        self.f_kwargs = f_kwargs if f_kwargs is not None else {}
        self.node = node if node is not None else local_client
        self.cluster_items = wazuh.core.cluster.utils.get_cluster_items() if node is None else node.cluster_items
        self.debug = debug
        self.node_info = wazuh.core.cluster.cluster.get_node() if node is None else node.get_node()
        self.request_id = str(random.randint(0, 2**10 - 1))
        self.request_type = request_type
        self.wait_for_complete = wait_for_complete
        self.from_cluster = from_cluster
        self.is_async = is_async
        self.broadcasting = broadcasting
        self.rbac_permissions = rbac_permissions if rbac_permissions is not None else dict()
        self.current_user = current_user
        self.nodes = nodes if nodes is not None else list()
        if not basic_services:
            self.basic_services = ('wazuh-modulesd', 'ossec-analysisd', 'ossec-execd', 'wazuh-db')
            if common.install_type != "local":
                self.basic_services += ('ossec-remoted',)
        else:
            self.basic_services = basic_services

        self.local_clients = []
        self.local_client_arg = local_client_arg
        self.threadpool = ThreadPoolExecutor(max_workers=1)

    async def distribute_function(self) -> [Dict, exception.WazuhException]:
        """
        Distributes an API call

        :return: Dictionary with API response or WazuhException in case of error
        """
        try:
            self.logger.debug("Receiving parameters {}".format(self.f_kwargs))
            is_dapi_enabled = self.cluster_items['distributed_api']['enabled']
            is_cluster_disabled = self.node == local_client and wazuh.core.cluster.cluster.check_cluster_status()

            # If it is a cluster API request and the cluster is not enabled, raise an exception
            if is_cluster_disabled and 'cluster' in self.f.__name__ and \
                    self.f.__name__ != '/cluster/status' and \
                    self.f.__name__ != '/cluster/config' and \
                    self.f.__name__ != '/cluster/node':
                raise exception.WazuhError(3013)

            # First case: execute the request local.
            # If the distributed api is not enabled
            # If the cluster is disabled or the request type is local_any
            # if the request was made in the master node and the request type is local_master
            # if the request came forwarded from the master node and its type is distributed_master

            if not is_dapi_enabled or is_cluster_disabled or self.request_type == 'local_any' or \
                    (self.request_type == 'local_master' and self.node_info['type'] == 'master') or \
                    (self.request_type == 'distributed_master' and self.from_cluster):

                response = await self.execute_local_request()

            # Second case: forward the request
            # Only the master node will forward a request, and it will only be forwarded if its type is distributed_
            # master
            elif self.request_type == 'distributed_master' and self.node_info['type'] == 'master':
                response = await self.forward_request()

            # Last case: execute the request remotely.
            # A request will only be executed remotely if it was made in a worker node and its type isn't local_any
            else:
                response = await self.execute_remote_request()

            try:
                response = json.loads(response, object_hook=c_common.as_wazuh_object) \
                    if isinstance(response, str) else response
            except json.decoder.JSONDecodeError:
                response = {'message': response}

            return response if isinstance(response, (wresults.AbstractWazuhResult, exception.WazuhException)) \
                else wresults.WazuhResult(response)

        except exception.WazuhError as e:
            e.dapi_errors = self.get_error_info(e)
            return e
        except exception.WazuhInternalError as e:
            e.dapi_errors = self.get_error_info(e)
            if self.debug:
                raise
            self.logger.error(f'{e.message}', exc_info=True)
            return e
        except Exception as e:
            if self.debug:
                raise

            self.logger.error(f'Unhandled exception: {str(e)}', exc_info=True)
            return exception.WazuhInternalError(1000,
                                                dapi_errors=self.get_error_info(e))

    def check_wazuh_status(self):
        """
        There are some services that are required for wazuh to correctly process API requests. If any of those services
        is not running, the API must raise an exception indicating that:
            * It's not ready yet to process requests if services are restarting
            * There's an error in any of those services that must be adressed before using the API if any service is
              in failed status.
            * Wazuh must be started before using the API is the services are stopped.

        The basic services wazuh needs to be running are: wazuh-modulesd, ossec-remoted, ossec-analysisd, ossec-execd
        and wazuh-db
        """
        if self.f == wazuh.core.manager.status:
            return

        status = wazuh.core.manager.status()

        not_ready_daemons = {k: status[k] for k in self.basic_services if status[k] in ('failed',
                                                                                        'restarting',
                                                                                        'stopped')}

        if not_ready_daemons:
            extra_info = {
                'node_name': self.node_info.get('node', 'UNKNOWN NODE'),
                'not_ready_daemons': ', '.join([f'{key}->{value}' for key, value in not_ready_daemons.items()])
            }
            raise exception.WazuhError(1017, extra_message=extra_info)

    async def execute_local_request(self) -> str:
        """
        Executes an API request locally.

        :return: a JSON response.
        """

        def run_local():
            self.logger.debug("Starting to execute request locally")
            common.rbac_mode.set(self.rbac_permissions.pop('rbac_mode', 'white'))
            common.rbac.set(self.rbac_permissions)
            common.broadcast.set(self.broadcasting)
            common.cluster_nodes.set(self.nodes)
            common.current_user.set(self.current_user)
            data = self.f(**self.f_kwargs)
            common.reset_context_cache()
            self.logger.debug("Finished executing request locally")
            return data

        try:
            before = time.time()

            self.check_wazuh_status()

            timeout = None if self.wait_for_complete \
                else self.cluster_items['intervals']['communication']['timeout_api_exe']

            # LocalClient only for control functions
            if self.local_client_arg is not None:
                lc = local_client.LocalClient()
                self.f_kwargs[self.local_client_arg] = lc
            else:
                lc = None

            if self.is_async:
                task = run_local()
            else:
                loop = asyncio.get_running_loop()
                task = loop.run_in_executor(self.threadpool, run_local)

            try:
                data = await asyncio.wait_for(task, timeout=timeout)
            except asyncio.TimeoutError:
                raise exception.WazuhException(3021)

            after = time.time()
            self.logger.debug("Time calculating request result: {}s".format(after - before))
            return data
        except exception.WazuhError as e:
            e.dapi_errors = self.get_error_info(e)
            if self.debug:
                raise
            return json.dumps(e, cls=c_common.WazuhJSONEncoder)
        except exception.WazuhInternalError as e:
            e.dapi_errors = self.get_error_info(e)
            self.logger.error(f"{e.message}", exc_info=True)
            if self.debug:
                raise
            return json.dumps(e, cls=c_common.WazuhJSONEncoder)
        except Exception as e:
            self.logger.error(f'Error executing API request locally: {str(e)}', exc_info=True)
            if self.debug:
                raise
            return json.dumps(exception.WazuhInternalError(1000,
                                                           dapi_errors=self.get_error_info(e)),
                              cls=c_common.WazuhJSONEncoder)

    def release_local_clients(self):
        """
        Close all local clients created.

        This method should only be called when all local clients connected to the LocalServer have finished sending
        requests. Otherwise, errors will arise on subsequent requests.
        """
        for lc in self.local_clients:
            lc.transport.close()

    def get_client(self) -> c_common.Handler:
        """
        Create another LocalClient if necessary and stores it to be closed later.

        :return: client. Maybe an instance of LocalClient, WorkerHandler or MasterHandler
        """
        if self.node == local_client:
            client = local_client.LocalClient()
            self.local_clients.append(client)
        else:
            client = self.node

        return client

    def to_dict(self):
        return {"f": self.f,
                "f_kwargs": self.f_kwargs,
                "request_type": self.request_type,
                "wait_for_complete": self.wait_for_complete,
                "from_cluster": self.from_cluster,
                "is_async": self.is_async,
                "local_client_arg": self.local_client_arg,
                "basic_services": self.basic_services,
                "rbac_permissions": self.rbac_permissions,
                "current_user": self.current_user,
                "broadcasting": self.broadcasting,
                "nodes": self.nodes
                }

    def get_error_info(self, e) -> Dict:
        """
        Builds a response given an Exception

        :param e: Exception

        :return: dict where keys are nodes and values are error information
        """
        error_message = e.message if isinstance(e, exception.WazuhException) else exception.GENERIC_ERROR_MSG
        result = {self.node_info['node']: {'error': error_message}
                  }

        # Give log path only in case of WazuhInternalError
        if not isinstance(e, exception.WazuhError):
            log_filename = None
            for h in self.logger.handlers or self.logger.parent.handlers:
                if hasattr(h, 'baseFilename'):
                    log_filename = os.path.join('WAZUH_HOME', os.path.relpath(h.baseFilename, start=common.ossec_path))
            result[self.node_info['node']]['logfile'] = log_filename

        return result

    async def send_tmp_file(self, node_name=None):
        # POST/agent/group/:group_id/configuration and POST/agent/group/:group_id/file/:file_name API calls write
        # a temporary file in /var/ossec/tmp which needs to be sent to the master before forwarding the request
        client = self.get_client()
        res = json.loads(await client.send_file(os.path.join(common.ossec_path,
                                                             self.f_kwargs['tmp_file']),
                                                node_name),
                         object_hook=c_common.as_wazuh_object)
        os.remove(os.path.join(common.ossec_path, self.f_kwargs['tmp_file']))

    async def execute_remote_request(self) -> Dict:
        """
        Executes a remote request. This function is used by worker nodes to execute master_only API requests.

        :return: JSON response
        """
        if 'tmp_file' in self.f_kwargs:
            await self.send_tmp_file()

        client = self.get_client()
        node_response = await client.execute(command=b'dapi',
                                             data=json.dumps(self.to_dict(),
                                                             cls=c_common.WazuhJSONEncoder).encode(),
                                             wait_for_complete=self.wait_for_complete)

        self.release_local_clients()

        return json.loads(node_response,
                          object_hook=c_common.as_wazuh_object)

    async def forward_request(self) -> [wresults.AbstractWazuhResult, exception.WazuhException]:
        """
        Forwards a request to the node who has all available information to answer it. This function is called when a
        distributed_master function is used. Only the master node calls this function. An API request will only be
        forwarded to worker nodes.

        :return: a JSON response.
        """

        async def forward(node_name: Tuple) -> [wresults.AbstractWazuhResult, exception.WazuhException]:
            """
            Forwards a request to a node.
            :param node_name: Node to forward a request to.
            :return: a JSON response
            """
            node_name, agent_list = node_name
            if agent_list:
                self.f_kwargs['agent_id' if 'agent_id' in self.f_kwargs else 'agent_list'] = agent_list
            if node_name == 'unknown' or node_name == '' or node_name == self.node_info['node']:
                # The request will be executed locally if the the node to forward to is unknown, empty or the master
                # itself
                result = await self.distribute_function()
            else:
                if 'tmp_file' in self.f_kwargs:
                    await self.send_tmp_file(node_name)
                client = self.get_client()
                try:
                    result = json.loads(await client.execute(b'dapi_forward',
                                                             "{} {}".format(node_name,
                                                                            json.dumps(self.to_dict(),
                                                                                       cls=c_common.WazuhJSONEncoder)
                                                                            ).encode(),
                                                             self.wait_for_complete),
                                        object_hook=c_common.as_wazuh_object)
                except WazuhClusterError as e:
                    if e.code == 3022:
                        result = e
                    else:
                        raise e
                # Convert a non existing node into a WazuhError exception
                if isinstance(result, WazuhClusterError) and result.code == 3022:
                    result = WazuhError.from_dict(result.to_dict())

            return result if isinstance(result, (wresults.AbstractWazuhResult, exception.WazuhException)) \
                else wresults.WazuhResult(result)

        # get the node(s) who has all available information to answer the request.
        nodes = await self.get_solver_node()
        self.from_cluster = True

        if len(nodes) > 1:
            results = await asyncio.shield(asyncio.gather(*[forward(node) for node in nodes.items()]))

            response = reduce(or_, results)
            if isinstance(response, wresults.AbstractWazuhResult):
                response = response.limit(limit=self.f_kwargs.get('limit', common.database_limit),
                                          offset=self.f_kwargs.get('offset', 0)) \
                    .sort(fields=self.f_kwargs.get('fields', []),
                          order=self.f_kwargs.get('order', 'asc'))
        else:
            response = await forward(next(iter(nodes.items())))

        self.release_local_clients()

        return response

    async def get_solver_node(self) -> Dict:
        """ Gets the node(s) that can solve a request

        Get the node(s) that have all the necessary information to answer the request. Only called when the request type
        is 'master_distributed' and the node_type is master.

        :return: List of node names with agents
        """
        select_node = ['node_name']
        if 'agent_id' in self.f_kwargs or 'agent_list' in self.f_kwargs:
            # Group requested agents by node_name
            requested_agents = self.f_kwargs.get('agent_list', None) or [self.f_kwargs['agent_id']]
            filters = {'id': requested_agents} if requested_agents != '*' else None
            system_agents = agent.Agent.get_agents_overview(select=select_node,
                                                            limit=None,
                                                            filters=filters,
                                                            sort={'fields': ['node_name'], 'order': 'desc'})['items']
            node_name = {k: list(map(operator.itemgetter('id'), g)) for k, g in
                         itertools.groupby(system_agents, key=operator.itemgetter('node_name'))}

            if requested_agents != '*':  # When all agents are requested cannot be non existent ids
                # Add non existing ids in the master's dictionary entry
                non_existent_ids = list(set(requested_agents) - set(map(operator.itemgetter('id'), system_agents)))
                if non_existent_ids:
                    if self.node_info['node'] in node_name:
                        node_name[self.node_info['node']].extend(non_existent_ids)
                    else:
                        node_name[self.node_info['node']] = non_existent_ids

            return node_name

        elif 'node_id' in self.f_kwargs or ('node_list' in self.f_kwargs and self.f_kwargs['node_list'] != '*'):
            requested_nodes = self.f_kwargs.get('node_list', None) or [self.f_kwargs['node_id']]
            del self.f_kwargs['node_id' if 'node_id' in self.f_kwargs else 'node_list']
            return {node_id: [] for node_id in requested_nodes}

        elif 'group_id' in self.f_kwargs:
            common.rbac_mode.set(self.rbac_permissions.pop('rbac_mode', 'white'))
            common.rbac.set(self.rbac_permissions)
            agents = agent.get_agents_in_group(group_list=[self.f_kwargs['group_id']], select=select_node,
                                               sort={'fields': ['node_name'], 'order': 'desc'}).affected_items
            if len(agents) == 0:
                raise WazuhError(1755)
            del self.f_kwargs['group_id']
            node_name = {k: list(map(operator.itemgetter('id'), g)) for k, g in
                         itertools.groupby(agents, key=operator.itemgetter('node_name'))}

            return node_name

        else:
            if self.broadcasting:
                if 'node_list' in self.f_kwargs:
                    del self.f_kwargs['node_list']
                client = self.get_client()
                nodes = json.loads(await client.execute(command=b'get_nodes',
                                                        data=json.dumps({}).encode(),
                                                        wait_for_complete=False),
                                   object_hook=c_common.as_wazuh_object)
                node_name = {item['name']: [] for item in nodes['items']}
            else:
                # agents, syscheck, rootcheck and syscollector
                # API calls that affect all agents. For example, PUT/agents/restart, DELETE/rootcheck, etc...
                agents = agent.Agent.get_agents_overview(select=select_node, limit=None,
                                                         sort={'fields': ['node_name'], 'order': 'desc'})['items']
                node_name = {k: [] for k, _ in itertools.groupby(agents, key=operator.itemgetter('node_name'))}
            return node_name


class APIRequestQueue:
    """
    Represents a queue of API requests. This thread will be always in background, it will remain blocked until a
    request is pushed into its request_queue. Then, it will answer the request and get blocked again.
    """

    def __init__(self, server):
        self.request_queue = asyncio.Queue()
        self.server = server
        self.logger = logging.getLogger('wazuh').getChild('dapi')
        self.logger.addFilter(wazuh.core.cluster.utils.ClusterFilter(tag='Cluster', subtag='D API'))
        self.pending_requests = {}

    async def run(self):
        while True:
            names, request = (await self.request_queue.get()).split(' ', 1)
            names = names.split('*', 1)
            # name    -> node name the request must be sent to. None if called from a worker node.
            # id      -> id of the request.
            # request -> JSON containing request's necessary information
            name_2 = '' if len(names) == 1 else names[1] + ' '

            # Get reference to MasterHandler or WorkerHandler
            node = self.server.client if names[0] == 'master' else self.server.clients[names[0]]
            try:
                request = json.loads(request, object_hook=c_common.as_wazuh_object)
                self.logger.info("Receiving request: {} from {}".format(
                    request['f'].__name__, names[0] if not name_2 else '{} ({})'.format(names[0], names[1])))

                result = await DistributedAPI(**request,
                                              logger=self.logger,
                                              node=node).distribute_function()
                task_id = await node.send_string(json.dumps(result, cls=c_common.WazuhJSONEncoder).encode())
            except Exception as e:
                self.logger.error("Error in distributed API: {}".format(e), exc_info=True)
                task_id = b'Error in distributed API: ' + str(e).encode()

            if task_id.startswith(b'Error'):
                self.logger.error(task_id.decode())
                result = await node.send_request(b'dapi_err', name_2.encode() + task_id)
            else:
                result = await node.send_request(b'dapi_res', name_2.encode() + task_id)
            if not isinstance(result, WazuhException):
                if result.startswith(b'Error'):
                    self.logger.error(result.decode())
            else:
                self.logger.error(result.message)

    def add_request(self, request: bytes):
        """
        Adds request to the queue

        :param request: Request to add
        """
        self.logger.debug("Received request: {}".format(request))
        self.request_queue.put_nowait(request.decode())
