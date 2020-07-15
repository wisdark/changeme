import argparse
from cerberus import Validator
from changeme.redis_queue import RedisQueue
import logging
from logutils import colorize
import os
import re
import redis
from .report import Report
import requests
from requests import ConnectionError
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from .scan_engine import ScanEngine
from . import schema
import sys
from . import version
import yaml

PERSISTENT_QUEUE = "data.db" # Instantiated in the scan_engine class


def banner(version):
    b = """
 #####################################################
#       _                                             #
#   ___| |__   __ _ _ __   __ _  ___ _ __ ___   ___   #
#  / __| '_ \ / _` | '_ \ / _` |/ _ \ '_ ` _ \ / _ \\  #
# | (__| | | | (_| | | | | (_| |  __/ | | | | |  __/  #
#  \___|_| |_|\__,_|_| |_|\__, |\___|_| |_| |_|\___|  #
#                         |___/                       #
#  v%s                                             #
#  Default Credential Scanner by @ztgrace             #
 #####################################################
    """ % version

    return b


def main():
    print(banner(version.__version__))

    args = parse_args()
    init_logging(args['args'].verbose, args['args'].debug, args['args'].log)
    config = Config(args['args'], args['parser'])
    if not config.noversion:
        check_version()
    creds = load_creds(config)
    s = None

    if config.mkcred:
        schema.mkcred()
        quit()

    if config.contributors:
        print_contributors(creds)
        quit()

    if config.dump:
        print_creds(creds)
        quit()

    logger = logging.getLogger('changeme')

    if not config.validate:
        check_for_interrupted_scan(config)
        s = ScanEngine(creds, config)
        try:
            s.scan()
        except IOError:
            logger.debug('Caught IOError exception')

        report = Report(s.found_q, config.output)
        report.print_results()

        if config.output and ".json" in config.output or config.output and config.oa:
            report.render_json()
        if config.output and ".csv" in config.output or config.output and config.oa:
            report.render_csv()
        if config.output and ".html" in config.output or config.output and config.oa:
            report.render_html()
        if (config.output and not ('json' in config.output or 'csv' in config.output or 'html' in config.output)) and not config.oa:
            logger.error('Only JSON, CSV and HTML are the only supported output types.')


    return s


def init_logging(verbose=False, debug=False, logfile=None):
    """
    Logging levels:
        - Critical: Default credential found
        - Error: error in the program
        - Warning: Verbose data
        - Info: more verbose
        - Debug: Extra info for debugging purposes
    """
    # Set up our logging object
    logger = logging.getLogger('changeme')

    if debug:
        logger.setLevel(logging.DEBUG)
    elif verbose:
        logger.setLevel(logging.INFO)
    else:
        logger.setLevel(logging.WARNING)

    if logfile:
        # Create file handler which logs even debug messages
        #######################################################################
        fh = logging.FileHandler(logfile)

        # create formatter and add it to the handler
        formatter = logging.Formatter(
            '[%(asctime)s][%(levelname)s] %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    # Set up the StreamHandler so we can write to the console
    ###########################################################################
    # create console handler with a higher log level
    ch = colorize.ColorizingStreamHandler(sys.stdout)

    # set custom colorings:
    ch.level_map[logging.DEBUG] = [None, 2, False]
    ch.level_map[logging.INFO] = [None, 'white', False]
    ch.level_map[logging.WARNING] = [None, 'yellow', False]
    ch.level_map[logging.ERROR] = [None, 'red', False]
    ch.level_map[logging.CRITICAL] = [None, 'green', False]
    if debug:
        formatter = logging.Formatter('[%(asctime)s][%(module)s][%(funcName)s] %(message)s', datefmt='%H:%M:%S')
    else:
        formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%H:%M:%S')
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # Adjust the loggers for requests and urllib3
    logging.getLogger('requests').setLevel(logging.ERROR)
    logging.getLogger('urllib3').setLevel(logging.ERROR)
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    return logger


class Config(object):
    def __init__(self, args, arg_parser):
        # Convert argparse Namespace to a dict and make the keys + values member variables of the config class
        args = vars(args)
        self.target = None
        for key in args:
            setattr(self, key, args[key])

        self._validate_args(arg_parser)

    def _validate_args(self, ap):
        logger = logging.getLogger('changeme')
        if (not self.validate and not self.contributors and not self.dump and not self.shodan_query
            and not self.mkcred and not self.resume) and not self.target:
            ap.print_help()
            quit()

        if self.proxy and re.match('^https?://[0-9\.]+:[0-9]{1,5}$', self.proxy):
            self.proxy = {'http': self.proxy, 'https': self.proxy}
            logger.info('Setting proxy to %s' % self.proxy)
        elif self.proxy:
            logger.error('Invalid proxy, must be http(s)://x.x.x.x:8080')
            sys.exit()

        if self.delay and self.delay != 0:
            if isinstance(self.delay, int) and 0 <= self.delay <= 1000:
                logger.debug('Delay is set to %d milliseconds' % self.delay)
            else:
                logger.error('Invalid delay type. Delay must be an integer between 0 and 1000.  Delay is: %s' %
                             type(self.delay))

        # Drop logging level to INFO to see the fingerprint messages
        if self.fingerprint:
            logger.setLevel(logging.INFO)

        if self.verbose:
            logger.setLevel(logging.INFO)
        if self.debug or self.validate:
            logger.setLevel(logging.DEBUG)

        self.useragent = {'User-Agent': self.useragent} if self.useragent else {}

        if ',' in self.protocols:
            self.protocols = self.protocols.split(',')

        if self.all:
            self.protocols = 'all'

        logger.debug(self.protocols)

        if self.output and which('phantomjs') is None:
            logger.warning('phantomjs is not in your path, screenshots will not work')


    def _file_exists(self, f):
        if not os.path.isfile(f):
            self.logger.error("File %s not found" % f)
            sys.exit()


def parse_args():
    ap = argparse.ArgumentParser(description='Default credential scanner v%s' % version.__version__)
    ap.add_argument('--all', '-a', action='store_true', help='Scan for all protocols', default=False)
    ap.add_argument('--category', '-c', type=str, help='Category of default creds to scan for', default=None)
    ap.add_argument('--contributors', action='store_true', help='Display cred file contributors')
    ap.add_argument('--debug', '-d', action='store_true', help='Debug output')
    ap.add_argument('--delay', '-dl', type=int, help="Specify a delay in milliseconds to avoid 429 status codes default=500", default=500)
    ap.add_argument('--dump', action='store_true', help='Print all of the loaded credentials')
    ap.add_argument('--dryrun', action='store_true', help='Print urls to be scan, but don\'t scan them')
    ap.add_argument('--fingerprint', '-f', action='store_true', help='Fingerprint targets, but don\'t check creds', default=False)
    ap.add_argument('--fresh', action='store_true', help='Flush any previous scans and start fresh', default=False)
    ap.add_argument('--log', '-l', type=str, help='Write logs to logfile', default=None)
    ap.add_argument('--mkcred', action='store_true', help='Make cred file', default=False)
    ap.add_argument('--name', '-n', type=str, help='Narrow testing to the supplied credential name', default=None)
    ap.add_argument('--noversion', action='store_true', help='Don\'t perform a version check', default=False)
    ap.add_argument('--proxy', '-p', type=str, help='HTTP(S) Proxy', default=None)
    ap.add_argument('--output', '-o', type=str, help='Name of result file. File extension determines type (csv, html, json).', default=None)
    ap.add_argument('--oa', action='store_true', help='Output results files in csv, html and json formats', default=False)
    ap.add_argument('--protocols', type=str, help="Comma separated list of protocols to test: http,ssh,ssh_key. Defaults to http.", default='http')
    ap.add_argument('--portoverride', action='store_true', help='Scan all protocols on all specified ports', default=False)
    ap.add_argument('--redishost', type=str, help='Redis server', default='localhost')
    ap.add_argument('--redisport', type=str, help='Redis server', default='6379')
    ap.add_argument('--resume', '-r', action='store_true', help='Resume previous scan', default=False)
    ap.add_argument('--shodan_query', '-q', type=str, help='Shodan query', default=None)
    ap.add_argument('--shodan_key', '-k', type=str, help='Shodan API key', default=None)
    ap.add_argument('--ssl', action='store_true', help='Force cred to SSL and fall back to non-SSL if an SSLError occurs', default=False)
    ap.add_argument('--threads', '-t', type=int, help='Number of threads, default=10', default=10)
    ap.add_argument('--timeout', type=int, help='Timeout in seconds for a request, default=10', default=10)
    ap.add_argument('--useragent', '-ua', type=str, help="User agent string to use", default=None)
    ap.add_argument('--validate', action='store_true', help='Validate creds files', default=False)
    ap.add_argument('--verbose', '-v', action='store_true', help='Verbose output', default=False)

    # Hack to get the help to show up right
    cli = ' '.join(sys.argv)
    if "-h" in cli or "--help" in cli:
        ap.add_argument('target', type=str, help='Target to scan. Can be IP, subnet, hostname, nmap xml file, text file or proto://host:port', default=None)

    # initial parse to see if an option not requiring a target was used
    args, unknown = ap.parse_known_args()
    if not args.dump and not args.contributors and not args.mkcred and not args.resume and not args.shodan_query and not args.validate:
        ap.add_argument('target', type=str, help='Target to scan. Can be IP, subnet, hostname, nmap xml file, text file or proto://host:port', default=None)

    args = ap.parse_args()

    return {'args': args, 'parser': ap}


def get_protocol(filename):
    parts = filename.split(os.path.sep)
    cred_index = 0
    for p in parts:
        if p == 'creds':
            break
        cred_index += 1

    return parts[cred_index + 1]



def load_creds(config):
    # protocol is based off of the directory and category is a field in the cred file. That way you can
    # have default creds across protocols for a single device like a printer
    logger = logging.getLogger('changeme')
    creds = list()
    total_creds = 0
    cred_names = list()
    cred_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'creds')
    logger.debug('cred_path: %s' % cred_path)
    protocols = [proto for proto in os.walk(cred_path)][0][1]
    for root, dirs, files in os.walk(cred_path):
        for fname in files:
            f = os.path.join(root, fname)
            protocol = get_protocol(f)
            if is_yaml(f):
                parsed = parse_yaml(f)
                if parsed:
                    if parsed['name'] in cred_names:
                        pass
                    elif validate_cred(parsed, f, protocol):
                        parsed['protocol'] = protocol  # Add the protocol after the schema validation
                        if in_scope(config.name, config.category, parsed, protocols):
                            total_creds += len(parsed['auth']['credentials'])
                            creds.append(parsed)
                            cred_names.append(parsed['name'])
                            logger.debug('Loaded %s' % parsed['name'])

    print('Loaded %i default credential profiles' % len(creds))
    print('Loaded %i default credentials\n' % total_creds)

    creds
    return creds


def validate_cred(cred, f, protocol):
    valid = True
    if protocol == 'http':
        v = Validator()
        valid = v.validate(cred, schema.http_schema)
        for e in v.errors:
            logging.getLogger('changeme').error("[validate_cred] Validation Error: %s, %s - %s" %
                                                (f, e, v.errors[e]))
    # TODO: implement schema validators for other protocols

    return valid


def parse_yaml(f):
    logger = logging.getLogger('changeme')
    with open(f, 'r') as fin:
        raw = fin.read()
        try:
            parsed = yaml.safe_load(raw)
        except Exception as e:
            logger.error("[parse_yaml] %s is not a valid yaml file" % f)
            logger.debug(e)
            return None
    return parsed


def is_yaml(f):
    isyaml = False
    try:
        isyaml = os.path.basename(f).split('.')[1] == 'yml'
    except:
        pass
    return isyaml


def in_scope(name, category, cred, protocols):
    add = True

    if name:
        names = name.split(',')
        found = False
        for n in names:
            if n.lower() in cred['name'].lower():
                found = True

        if found is False:
            add = False

    if category and not cred['category'] == category:
        add = False
    elif cred['protocol'] not in protocols:
        add = False

    return add


def print_contributors(creds):
    contributors = set()
    for cred in creds:
        cred_contributors = cred['contributor'].split(', ')
        for c in cred_contributors:
            contributors.add(c)

    for c in version.contributors:
        contributors.add(c)

    print("Thank you to our contributors!")
    for i in sorted(contributors, key=str.lower):
        print(i)
    print()


def print_creds(creds):
    for cred in creds:
        print("\n%s (%s)" % (cred['name'], cred['category']))
        for i in cred['auth']['credentials']:
            print("  - %s:%s" % (i['username'], i['password']))


def check_for_interrupted_scan(config):
    logger = logging.getLogger('changeme')
    if config.fresh:
        logger.debug("Forcing a fresh scan")
        remove_queues()
    elif config.resume:
        logger.debug("Resuming previous scan")
        return

    fp = RedisQueue('fingerprint')
    scanners = RedisQueue('scanners')
    fp_qsize = 0
    scanners_qsize = 0
    logger.debug('scanners: %s, fp: %s' % (scanners_qsize, fp_qsize))
    try:
        fp_qsize = fp.qsize()
    except redis.exceptions.ConnectionError:
        pass
    try:
        scanners_qsize = scanners.qsize()
    except redis.exceptions.ConnectionError:
        pass

    if fp_qsize > 0 or scanners_qsize > 0:
        if not prompt_for_resume(config):
            remove_queues()

    if fp_qsize == 0 and scanners_qsize == 0:
        # Clear the found queue if there's no fingerprints or scanners in the queues
        try:
            logger.debug('Clearing found_q')
            found_q = RedisQueue('found_q')
            found_q.delete()
        except Exception as e:
            logger.debug('Exception: %s: %s' % (type(e).__name__, e.__str__().replace('\n', '|')))
            pass



def prompt_for_resume(config):
    logger = logging.getLogger('changeme')
    logger.error('A previous scan was interrupted. Type R to resume or F to start a fresh scan')
    answer = ''
    while not (answer == 'R' or answer == 'F'):
        prompt = '(R/F)> '
        answer = ''
        try:
            answer = raw_input(prompt)
        except NameError:
            answer = input(prompt)

        if answer.upper() == 'F':
            logger.debug("Forcing a fresh scan")
        elif answer.upper() == 'R':
            logger.debug("Resuming previous scan")
            config.resume = True

    return config.resume


def remove_queues():
    logger = logging.getLogger('changeme')
    try:
        os.remove(PERSISTENT_QUEUE)
        logger.debug("%s removed" % PERSISTENT_QUEUE)
    except OSError:
        logger.debug("%s didn't exist" % PERSISTENT_QUEUE)
        pass

    # Clear Redis
    queues = ['fingerprint', 'scanners', 'found_q']
    for q in queues:
        logger.debug('Removing %s RedisQueue' % q)
        r = RedisQueue(q)
        try:
            r.delete()
            logger.debug("%s Redis queue removed" % q)
        except:
            logger.debug("%s Redis queue exception" % q)
            pass


def check_version():
    logger = logging.getLogger('changeme')

    try:
        res = requests.get('https://raw.githubusercontent.com/ztgrace/changeme/master/changeme/version.py', timeout=2)
    except ConnectionError:
        logger.debug("Unable to retrieve latest changeme version.")
        return

    latest = res.text.split('\n')[0].split(' = ')[1].replace("'", '')
    if not version.__version__ == latest:
        logger.warning('Your version of changeme is out of date. Local version: %s, Latest: %s' % (str(version.__version__), latest))


# copied from https://stackoverflow.com/questions/377017/test-if-executable-exists-in-python
def which(program):
    import os

    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program):
            return program
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            path = path.strip('"')
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file

    return None

