'''
copyright: Copyright (C) 2015-2022, Wazuh Inc.

           Created by Wazuh, Inc. <info@wazuh.com>.

           This program is free software; you can redistribute it and/or modify it under the terms of GPLv2

type: integration

brief: These tests will check if the 'wazuh-authd' daemon correctly handles the enrollment requests,
       generating consistent responses to the requests received on its IP v4 network socket.
       The 'wazuh-authd' daemon can automatically add a Wazuh agent to a Wazuh manager and provide
       the key to the agent. It is used along with the 'agent-auth' application.

components:
    - authd

targets:
    - manager

daemons:
    - wazuh-authd
    - wazuh-db
    - wazuh-modulesd

os_platform:
    - linux

os_version:
    - Arch Linux
    - Amazon Linux 2
    - Amazon Linux 1
    - CentOS 8
    - CentOS 7
    - Debian Buster
    - Red Hat 8
    - Ubuntu Focal
    - Ubuntu Bionic

references:
    - https://documentation.wazuh.com/current/user-manual/reference/daemons/wazuh-authd.html
    - https://documentation.wazuh.com/current/user-manual/reference/tools/agent_groups.html

tags:
    - enrollment
'''
import os
import subprocess
import time

import pytest
from pathlib import Path

from . import CONFIGURATIONS_FOLDER_PATH, TEST_CASES_FOLDER_PATH
from wazuh_testing.utils.configuration import get_test_cases_data, load_configuration_template

# Marks

pytestmark = [pytest.mark.linux, pytest.mark.tier(level=0), pytest.mark.server]

# Paths
test_configuration_path = Path(CONFIGURATIONS_FOLDER_PATH, 'config_authd_common.yaml')
test_cases_path = Path(TEST_CASES_FOLDER_PATH, 'cases_authd.yaml')

# Configurations
test_configuration, test_metadata, test_cases_ids = get_test_cases_data(test_cases_path)
test_configuration = load_configuration_template(test_configuration_path, test_configuration, test_metadata)

# Variables
log_monitor_paths = []

receiver_sockets_params = [(("localhost", 1515), 'AF_INET', 'SSL_TLSv1_2')]

monitored_sockets_params = [('wazuh-modulesd', None, True), ('wazuh-db', None, True), ('wazuh-authd', None, True)]

receiver_sockets, monitored_sockets = None, None  # Set in the fixtures

daemons_handler_configuration = {'all_daemons': True, 'ignore_errors': True}


# Tests

@pytest.fixture(scope="function", params=test_metadata)
def set_up_groups(request):
    groups = test_metadata['groups']
    for group in groups:
        subprocess.call(['/var/ossec/bin/agent_groups', '-a', '-g', f'{group}', '-q'])
    yield request.param
    for group in groups:
        subprocess.call(['/var/ossec/bin/agent_groups', '-r', '-g', f'{group}', '-q'])


@pytest.mark.parametrize('test_configuration,test_metadata', zip(test_configuration, test_metadata), ids=test_cases_ids)
def test_ossec_auth_messages(test_configuration, test_metadata, set_wazuh_configuration, set_up_groups, configure_sockets_environment,
                             clean_client_keys_file_module, wait_for_authd_startup_module, connect_to_sockets_module, daemons_handler):
    '''
    description:
        Checks if when the `wazuh-authd` daemon receives different types of enrollment requests,
        it responds appropriately to them. In this case, the enrollment requests are sent to
        an IP v4 network socket.

    wazuh_min_version:
        4.2.0

    tier: 0

    parameters:
        - get_configuration:
            type: fixture
            brief: Get configurations from the module.
        - set_up_groups:
            type: fixture
            brief: Create a testing group for agents and provide the test case list.
        - configure_environment:
            type: fixture
            brief: Configure a custom environment for testing.
        - configure_sockets_environment:
            type: fixture
            brief: Configure environment for sockets and MITM.
        - clean_client_keys_file_module:
            type: fixture
            brief: Stops Wazuh and cleans any previous key in client.keys file at module scope.
        - restart_authd:
            type: fixture
            brief: Restart the 'wazuh-authd' daemon, clear the 'ossec.log' file and start a new file monitor.
        - wait_for_authd_startup_module:
            type: fixture
            brief: Waits until Authd is accepting connections.
        - connect_to_sockets_module:
            type: fixture
            brief: Module scope version of 'connect_to_sockets' fixture.


    assertions:
        - Verify that the response messages are consistent with the enrollment requests received.

    input_description:
        Different test cases are contained in an external `YAML` file (enroll_messages.yaml)
        that includes enrollment events and the expected output.

    expected_output:
        - Multiple values located in the `enroll_messages.yaml` file.

    tags:
        - keys
        - ssl
    '''
    test_case = test_metadata['stages']
    for stage in test_case:
        # Reopen socket (socket is closed by manager after sending message with client key)
        receiver_sockets[0].open()
        expected = stage['output']
        message = stage['input']
        receiver_sockets[0].send(message, size=False)
        timeout = time.time() + 10
        response = ''
        while response == '':
            response = receiver_sockets[0].receive().decode()
            if time.time() > timeout:
                assert response != '', 'The manager did not respond to the message sent.'
        assert response[:len(expected)] == expected, \
            'Failed test case {}: Response was: {} instead of: {}'.format(set_up_groups['name'], response, expected)
