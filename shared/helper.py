#!/usr/bin/env python
# -*- coding: utf-8 -*-
from time import time
import argparse
import json
import sys
import os
import logging
import operator
from copy import copy
from uuid import uuid4
import socket
import re
from redis import Redis
from operator import itemgetter

__version__ = '0.100.500'

LOG_FORMAT = '[%(levelname)s] %(name)s %(message)s'
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format=LOG_FORMAT,
                    datefmt='%H:%M:%S %d-%m-%Y')

LOGGER = logging.getLogger('helper')


def str2bool(s):
    yes_bools = ['true', 'yes', 'da', 'aga', 'ok', 'yep', 'да', 'ага', 'kk', 'y', 'конечно']

    if str(s).lower() in yes_bools:
        return True
    else:
        return False


def init_args():

    arg_parser = argparse.ArgumentParser(prog='helper', epilog='------',
                                         description='Auth shell helper')
    arg_parser.add_argument('action', type=str, nargs=1, choices=['search', 'go'])
    arg_parser.add_argument('sargs', type=str, nargs='+',
                            help='[search server_id | go project | go project server_name]')
    arg_parser.add_argument('--helper-debug', action='store_true')
    arg_parser.add_argument_group('Search', 's <query> [opts]')
    arg_parser.add_argument_group('Go', 'g <project|host> [server_name|server_ip] [opts]')

    # Unknown args bypassed to ssh.py wrapper
    args, unknown_args = arg_parser.parse_known_args()

    if args.helper_debug or '--debug' in sys.argv:
        args.helper_debug = True

        logging.basicConfig(stream=sys.stdout, level=logging.DEBUG,
                            format=LOG_FORMAT, datefmt='%Y-%m-%d %H:%M:%S')
        LOGGER.info('Helper debug mode on')
        LOGGER.info(sys.argv)
        LOGGER.info(vars(args))
        LOGGER.info(unknown_args)

    else:
        logging.basicConfig(stream=sys.stderr, level=logging.WARN, format=LOG_FORMAT, datefmt='%Y-%m-%d %H:%M:%S')

    return args, unknown_args


class ServerConnection(object):
    #
    helper = None
    #
    arg_type = None
    search_results = []
    project = None
    server_name = None
    server_id = None
    #
    # host config
    #
    host = None
    port = None
    user = None
    nosudo = None
    proxy_id = None
    #
    proxy_config = None
    #
    session_exports = list()
    session_file_path = os.getenv('AUTH_SESSION', None)
    session_exports.append('AUTH_CALLBACK="{}";'.format(session_file_path))
    ssh_wrapper_cmd = os.getenv('AUTH_WRAPPER', 'sudo -u auth /opt/auth/wrappers/ssh.py')

    #
    def __init__(self, helper=None, unknown_args=None):
        self.helper = helper
        self.unknown_args = unknown_args

    #
    # perform connection structure checks
    #
    def _validate(self):
        if len(self.search_results) > 1:
            raise Exception('passed more that one host in search_results')
        elif len(self.search_results) == 0:
            LOGGER.debug('ServerConnection.resolve: No hosts in search_results passed')

    #
    # host ssh_config [high]
    #
    def _get_host_config(self):
        if len(self.search_results) != 1:
            return
        host_config = self.search_results[0]

        if 'server_ip' in host_config.keys():
            self.host = host_config['server_ip']
        if 'server_port' in host_config.keys():
            self.port = host_config['server_port']
        if 'server_user' in host_config.keys():
            self.user = host_config['server_user']
        if 'server_nosudo' in host_config.keys():
            self.nosudo = host_config['server_nosudo']
        if 'proxy_id' in host_config.keys():
            self.proxy_id = host_config['proxy_id']


    #
    # resolve host configuratin
    #
    def resolve(self):
        self._get_host_config()

    #
    # build commands
    #
    def build_cmd(self):

        if self.host:
            self.ssh_wrapper_cmd += ' {}'.format(self.host)

        if self.port:
            self.ssh_wrapper_cmd += ' --port {}'.format(self.port)

        if self.user:
            self.ssh_wrapper_cmd += ' --user {}'.format(self.user)

        if self.nosudo:
            self.ssh_wrapper_cmd += ' --nosudo'

        if bool(self.unknown_args):
            self.ssh_wrapper_cmd += ' ' + ' '.join(self.unknown_args)

        self.session_exports.append('AUTH_CALLBACK_CMD="{}"'.format(self.ssh_wrapper_cmd))

    def _write_session(self):
        if self.session_file_path is None:
            return None

        with open(self.session_file_path, 'w') as sess_f:
            for line in self.session_exports:
                    sess_f.write(line + '\n')

    def start(self):
        self._validate()
        self.resolve()
        self.build_cmd()
        self._write_session()

        # debug
        self.__dict__.pop('helper', None)
        self.__dict__.pop('host_ssh_config', None)
        self.__dict__.pop('project_config', None)
        self.__dict__.pop('search_results', None)

        LOGGER.debug(self.__dict__)


class AuthHelper(object):

    def __init__(self, args, unknown_args):
        self.uuid = str(uuid4())
        self.projects = None
        self.time_start = time()
        self.args = args
        self.unknown_args = unknown_args
        self._init_env_vars()
        self.redis = Redis(host=os.getenv('AUTH_REDIS_IP', '127.0.0.1'),
                           port=int(os.getenv('AUTH_REDIS_PORT', 6379)),
                           password=os.getenv('AUTH_REDIS_PASS', 'te2uth4dohLi8i'),
                           db=0)
        self._load_data()
        LOGGER.debug('AuthHelper init done')

    def print_p(self, arg, stderr=False):
        try:
            if not stderr:
                sys.stdout.write(str(arg) + '\n')
                sys.stdout.flush()
            else:
                sys.stderr.write(str(arg) + '\n')
                sys.stderr.flush()
        except IOError:
            try:
                sys.stdout.close()
            except IOError:
                pass
            try:
                sys.stderr.close()
            except IOError:
                pass
                exit(0)

    @staticmethod
    def is_valid_ipv4(address):
        try:
            socket.inet_pton(socket.AF_INET, address)
        except AttributeError:  # no inet_pton here, sorry
            try:
                socket.inet_aton(address)
            except socket.error:
                return False
            return True
        except socket.error:  # not a valid address
            return False

        return True

    @staticmethod
    def is_valid_ipv6(address):
        try:
            socket.inet_pton(socket.AF_INET6, address)
        except socket.error:  # not a valid address
            return False
        return True

    @staticmethod
    def is_valid_fqdn(hostname):
        # FQDN must be pretty, without dots at start/end
        # and have one minimum
        hostname = str(hostname).lower()
        if len(hostname) > 255:
            return False
        if hostname[-1] == '.' or hostname[0] == '.' or '.' not in hostname:
            return False
        if re.match('^([a-z\d\-.]*)$', hostname) is None:
            return False
        return True

    def _init_env_vars(self):
        # Main config options
        self.AUTH_DATA_ROOT = os.getenv('AUTH_DATA_ROOT', '/opt/auth')
        self.AUTH_DEBUG = str2bool(os.getenv('AUTH_DEBUG', False))

        self.USER = os.getenv('USER', 'USER_ENV_NOT_SET')
        self.SUDO_USER = os.getenv('SUDO_USER', 'SUDO_USER_ENV_NOT_SET')
        self.AUTH_WRAPPER = os.getenv('AUTH_WRAPPER', 'sudo -u auth /mnt/data/auth/wrap/ssh.py')

        # User interface options
        # search print fields seporator
        self.AUTH_SPF_SEP = os.getenv('AUTH_SPF_SEP', ' | ')

        # Go to server immediately if only one server in group
        self.AUTH_BLINDE = str2bool(os.getenv('AUTH_BLINDE', False))

        # Colorize interface
        self.AUTH_COLORS = str2bool(os.getenv('AUTH_COLORS', False))

        # Search Print Line: fields names and order, not template
        self.AUTH_SPF = os.getenv('AUTH_SPF', 'server_id server_ip server_name').strip().split(' ')

    def _load_data(self):
        self.hosts_dump = []
        self.projects = []

        for server_key in self.redis.keys('server_*'):
            server_data = self.redis.get(server_key)
            server_data = json.loads(server_data)

            self.projects.append(server_data['project_name'])
            self.hosts_dump.append(server_data)

        self.projects = list(sorted(set(self.projects)))
        self.hosts_dump = sorted(self.hosts_dump, key=itemgetter('project_name'))

        LOGGER.debug('_load_data')
        LOGGER.debug(json.dumps(self.hosts_dump, indent=4))
        LOGGER.debug(json.dumps(self.projects, indent=4))

    def _search_in_item(self, **kwargs):
        item = kwargs.get('item')
        item_keys = item.keys()
        # query_src = kwargs.get('query_src')
        query_lower = kwargs.get('query_lower')

        # project_id - is bad idea
        fields = kwargs.get('fields', ['project_name',
                                       'project_id',
                                       'server_name',
                                       'server_id',
                                       'server_ip',
                                       'os_version',
                                       'asn'])  # 'alerts'

        exact_match = kwargs.get('exact_match', False)

        if exact_match:
            for key in fields:
                if key not in item_keys:
                    continue
                if query_lower == str(item[key]).lower():
                    item['exact_match'] = key
                    return item
        else:
            for key in fields:
                if key not in item_keys:
                    continue
                if query_lower in str(item[key]).lower():
                    item['match_by'] = key
                    return item
        return False

    def search(self, query, **kwargs):
        time_search_start = time()
        source = kwargs.pop('source', self.hosts_dump)
        project = kwargs.pop('project_name', False)
        kwargs.update(query_src=str(query), query_lower=query.lower())

        result = list()

        for item in source:
            # project filter
            if project:
                if item['project_name'] != project:
                    continue

            item_query = copy(kwargs)
            item_query['item'] = item

            res = self._search_in_item(**item_query)
            if bool(res):
                result.append(res)

        kwargs.pop('query_lower')
        kwargs.update(search_time=float(time() - time_search_start))

        if kwargs.get('sort'):
            result = sorted(result, key=operator.itemgetter(kwargs.get('sort')))

        LOGGER.debug((query, kwargs))

        return result

    def colorize(self, text, color=None):
        colors = dict(
            header='\033[95m',
            okblue='\033[94m',
            okgreen='\033[92m',
            warning='\033[93m',
            fail='\033[91m',
            reset='\033[0m',
            bold='\033[1m',
            underline='\033[4m',
            # fields colors
            project_name='\033[38;5;45m',
            group_name='\033[38;5;45m',
            blue='\033[38;5;45m',
            critical='\033[38;5;160m',
            green='\033[38;5;40m',
            old='\033[38;5;142m',
            warn='\033[38;5;142m',
            status='\033[38;5;40m',
            os_version='\033[38;5;220m'
        )

        if not self.AUTH_COLORS or not colors.get(color, False):
            return text
        else:
            return '{0}{1}{2}'.format(colors.get(color), text, colors.get('reset'))

    def ljust_algin(self, host, **kwargs):
        # Minimum field size (add spaces)
        ljust_size = {
            'project_name': 8,
            'server_name': 12,
            'server_id': 7,
            'last_ip': 16,
            'ssh_config_ip': 16
        }

        host = copy(host)
        for key in self.AUTH_SPF:
            if key not in host.keys():
                continue
            if host[key] in [None, True, False]:
                continue
            if len(str(host[key])) > 0 and key in self.AUTH_SPF:
                if key in ljust_size:
                    host[key] = str(host[key]).ljust(ljust_size[key], ' ')
                if host[key][-1] != ' ':
                    host[key] += ' '
        return host

    def append_virtual_fields(self, host, **kwargs):
        # Some helpful fields hook
        ambiguous = kwargs.get('ambiguous', False)
        host_keys = host.keys()

        if ambiguous:
            self.AUTH_SPF = ['server_id', 'match_info', 'exact_match', 'server_id', 'project_name',
                             'server_ip', 'server_name']

        # matching debug field
        if 'match_info' in self.AUTH_SPF:
            match_info = list()
            if 'match_by' in host_keys:
                match_info.append('by: {}'.format(host['match_by']))
            if 'exact_match' in host_keys:
                match_info.append('exact: {}'.format(host['exact_match']))

            host['match_info'] = self.colorize(', '.join(match_info), color='okgreen')

        return host

    def print_hosts(self, hosts, **kwargs):
        title = kwargs.get('title', True)  # Print project title by default
        total = kwargs.get('total', True)
        current_project = None  # stub
        counter = 0

        if len(hosts) == 0:
            ambiguous = True
        else:
            ambiguous = kwargs.get('ambiguous', False)  # Hosts list is ambiguous

        if ambiguous:
            if len(hosts) == 0:
                self.print_p(self.colorize('\n  No servers found by this query: ', color='warn')
                             + ' '.join(self.args.sargs))
            else:
                self.print_p(self.colorize('\n  Ambiguous, more than one server found '
                                           'by this query: \n', color='warn'))

        if not title:
            self.print_p('')

        for host in hosts:
            # project_name
            # ------
            # Title
            if current_project != host['project_name'] and title and not ambiguous:
                current_project = host['project_name']
                title_tpl = '\n{0}\n------'.format(self.colorize(current_project, color='project_name'))
                self.print_p(title_tpl)

            # Fields print prepare
            host = self.append_virtual_fields(host, ambiguous=ambiguous)
            host = self.ljust_algin(host)

            # Concat strings
            host_line = []

            for field in self.AUTH_SPF:
                if field in host.keys():
                    host_line.append(host[field])

            if ambiguous or not title:
                line = '  ' + self.AUTH_SPF_SEP.join(host_line)
            else:
                line = self.AUTH_SPF_SEP.join(host_line)

            self.print_p(line)
            counter += 1

        if ambiguous:
            total_tpl = '\n  Total: {0}\n'.format(counter)
            self.print_p(total_tpl)

        elif total:
            total_tpl = '\n------\nTotal: {0}\n'.format(counter)
            self.print_p(total_tpl)
        else:
            self.print_p('')


def main():
    args, unknown_args = init_args()
    helper = AuthHelper(args, unknown_args)

    conn = ServerConnection(helper=helper, unknown_args=unknown_args)

    if args.action[0] == 'search':
        LOGGER.debug('its global search action')
        LOGGER.debug(args)
        LOGGER.debug(unknown_args)

        if len(args.sargs) == 1 and args.sargs[0] in helper.projects:
            search_results = helper.search(args.sargs[0], fields=['project'], exact_match=True)
        elif len(args.sargs) == 2 and args.sargs[0] in helper.projects:
            search_results = helper.search(args.sargs[1], project=args.sargs[0])
        else:
            search_results = helper.search(' '.join(args.sargs))

        helper.print_hosts(search_results)

    elif args.action[0] == 'go':

        if len(args.sargs) > 2:
            LOGGER.critical('Unknown sargs, see --help in --helper-debug for details')
            sys.exit(1)

        #
        # Only one arg
        #
        # if its some ID (only digits) search in server_id fields
        if len(args.sargs) == 1 and args.sargs[0].isdigit():
            conn.arg_type = 'server_id_only'
            conn.search_results = helper.search(args.sargs[0], fields=['server_id'], exact_match=True)

            if len(conn.search_results) == 1:
                conn.project = conn.search_results[0]['project_name']
                conn.server_id = args.sargs[0]
                conn.start()
            else:
                helper.print_hosts(conn.search_results, ambiguous=True)

        elif len(args.sargs) == 1 and args.sargs[0] in helper.projects:
            conn.arg_type = 'project_only'
            conn.project = args.sargs[0]
            conn.search_results = helper.search(args.sargs[0], fields=['project_name'], exact_match=True)

            if len(conn.search_results) == 1 and helper.AUTH_BLINDE:
                conn.server_id = conn.search_results[0]['server_id']
                conn.start()
            else:
                helper.print_hosts(conn.search_results)

        # if arg is ipv4 or fqdn
        elif len(args.sargs) == 1 and helper.is_valid_ipv4(args.sargs[0]):
            conn.arg_type = 'ipv4_only'
            conn.host = args.sargs[0]
            conn.start()

        elif len(args.sargs) == 1 and helper.is_valid_fqdn(args.sargs[0]):
            LOGGER.debug('is_valid_fqdn')
            conn.arg_type = 'fqdn_only'
            conn.host = args.sargs[0]
            conn.start()

        #
        # Two arguments
        #
        # if first arg is project and second ... shit
        elif len(args.sargs) == 2 and args.sargs[0] in helper.projects:
            conn.arg_type = 'project_with_some_shit'
            conn.project = args.sargs[0]
            conn.search_results = helper.search(args.sargs[1], project_name=conn.project,
                                                fields=['server_name', 'server_id', 'server_ip'],
                                                exact_match=True)

            if len(conn.search_results) == 1:
                if args.sargs[1].isdigit():
                    conn.arg_type = 'project_with_server_id_found'
                    conn.server_id = conn.search_results[0]['server_id']

                elif helper.is_valid_ipv4(args.sargs[1]):
                    conn.arg_type = 'project_with_ipv4_found'
                    conn.server_id = conn.search_results[0]['server_id']
                    conn.host = conn.search_results[0]['server_ip']
                elif helper.is_valid_fqdn(args.sargs[1]):
                    conn.arg_type = 'project_with_fqdn_found'
                    conn.server_id = conn.search_results[0]['server_id']
                    conn.host = conn.search_results[0]['server_name']
                else:
                    conn.arg_type = 'project_with_server_name_found'
                    conn.server_id = conn.search_results[0]['server_id']
                    conn.server_name = conn.search_results[0]['server_name']
                conn.start()

            elif len(conn.search_results) == 0 and len(args.sargs[1]) >= 2:
                if helper.is_valid_ipv4(args.sargs[1]):
                    conn.arg_type = 'project_with_ipv4_host_not_found'
                    conn.start()
                elif helper.is_valid_fqdn(args.sargs[1]) and not args.sargs[1].isdigit() and '.' in args.sargs[1]:
                    conn.arg_type = 'project_with_fqdn_host_not_found'
                    conn.start()
                else:
                    helper.print_hosts(conn.search_results, ambiguous=True)
            else:
                helper.print_hosts(conn.search_results, ambiguous=True)
        else:
            LOGGER.critical('args not match')
    else:
        LOGGER.critical('Unknown action: ' + args.action[0])

    done_delta = round(time() - helper.time_start, 3)
    LOGGER.debug('run time: ' + str(done_delta) + ' sec')

if __name__ == '__main__':
    main()
