import demistomock as demisto
from CommonServerPython import *
from CommonServerUserPython import *

import re
from base64 import b64decode
from multiprocessing import Process
from gevent.pywsgi import WSGIServer
from tempfile import NamedTemporaryFile
from flask import Flask, Response, request
from netaddr import IPAddress, IPSet
from typing import Callable, List, Any, Dict, cast, Tuple
from ssl import SSLContext, SSLError, PROTOCOL_TLSv1_2


class Handler:
    @staticmethod
    def write(msg: str):
        demisto.info(msg)


class ErrorHandler:
    @staticmethod
    def write(msg: str):
        demisto.error(msg)


''' GLOBAL VARIABLES '''
INTEGRATION_NAME: str = 'EDL'
PAGE_SIZE: int = 2000
DEMISTO_LOGGER: Handler = Handler()
ERROR_LOGGER: ErrorHandler = ErrorHandler()
APP: Flask = Flask('demisto-edl')
EDL_VALUES_KEY: str = 'dmst_edl_values'
EDL_LIMIT_ERR_MSG: str = 'Please provide a valid integer for EDL Size'
EDL_OFFSET_ERR_MSG: str = 'Please provide a valid integer for Starting Index'
EDL_COLLAPSE_ERR_MSG: str = 'The Collapse parameter can only get the following: 0 - Dont Collapse, ' \
                            '1 - Collapse to Ranges, 2 - Collapse to CIDRS'
EDL_MISSING_REFRESH_ERR_MSG: str = 'Refresh Rate must be "number date_range_unit", examples: (2 hours, 4 minutes, ' \
                                   '6 months, 1 day, etc.)'
EDL_LOCAL_CACHE: dict = {}
''' REFORMATTING REGEXES '''
_PROTOCOL_REMOVAL = re.compile('^(?:[a-z]+:)*//')
_PORT_REMOVAL = re.compile(r'^((?:[a-z]+:)*//([a-z0-9\-\.]+)|([a-z0-9\-\.]+))(?:\:[0-9]+)*')
_URL_WITHOUT_PORT = r'\g<1>'
_INVALID_TOKEN_REMOVAL = re.compile(r'(?:[^\./+=\?&]+\*[^\./+=\?&]*)|(?:[^\./+=\?&]*\*[^\./+=\?&]+)')

DONT_COLLAPSE = "Don't Collapse"
COLLAPSE_TO_CIDR = "To CIDRS"
COLLAPSE_TO_RANGES = "To Ranges"

'''Request Arguments Class'''


class RequestArguments:
    def __init__(self,
                 query: str,
                 limit: int = 10000,
                 offset: int = 0,
                 url_port_stripping: bool = False,
                 drop_invalids: bool = False,
                 collapse_ips: str = DONT_COLLAPSE):

        self.query = query
        self.limit = limit
        self.offset = offset
        self.url_port_stripping = url_port_stripping
        self.drop_invalids = drop_invalids
        self.collapse_ips = collapse_ips

    def is_request_change(self, last_update_data: Dict):
        if self.limit != last_update_data.get('last_limit'):
            return True

        elif self.offset != last_update_data.get('last_offset'):
            return True

        elif self.drop_invalids != last_update_data.get('drop_invalids'):
            return True

        elif self.url_port_stripping != last_update_data.get('url_port_stripping'):
            return True

        elif self.collapse_ips != last_update_data.get('collapse_ips'):
            return True

        return False


''' HELPER FUNCTIONS '''


def list_to_str(inp_list: list, delimiter: str = ',', map_func: Callable = str) -> str:
    """
    Transforms a list to an str, with a custom delimiter between each list item
    """
    str_res = ""
    if inp_list:
        if isinstance(inp_list, list):
            str_res = delimiter.join(map(map_func, inp_list))
        else:
            raise AttributeError('Invalid inp_list provided to list_to_str')
    return str_res


def get_params_port(params: dict = demisto.params()) -> int:
    """
    Gets port from the integration parameters
    """
    port_mapping: str = params.get('longRunningPort', '')
    err_msg: str
    port: int
    if port_mapping:
        err_msg = f'Listen Port must be an integer. {port_mapping} is not valid.'
        if ':' in port_mapping:
            port = try_parse_integer(port_mapping.split(':')[1], err_msg)
        else:
            port = try_parse_integer(port_mapping, err_msg)
    else:
        raise ValueError('Please provide a Listen Port.')
    return port


def refresh_edl_context(request_args: RequestArguments, save_integration_context: bool = False) -> str:
    """
    Refresh the cache values and format using an indicator_query to call demisto.searchIndicators

    Parameters:
        request_args: Request arguments
        save_integration_context: Flag to save the result to integration context instead of LOCAL_CACHE

    Returns: List(IoCs in output format)
    """
    now = datetime.now()
    # poll indicators into edl from demisto
    iocs = find_indicators_to_limit(request_args.query, request_args.limit, request_args.offset)
    out_dict, actual_indicator_amount = create_values_for_returned_dict(iocs, request_args)

    while actual_indicator_amount < request_args.limit:
        # from where to start the new poll and how many results should be fetched
        new_offset = len(iocs) + request_args.offset
        new_limit = request_args.limit - actual_indicator_amount

        # poll additional indicators into list from demisto
        new_iocs = find_indicators_to_limit(request_args.query, new_limit, new_offset)

        # in case no additional indicators exist - exit
        if len(new_iocs) == 0:
            break

        # add the new results to the existing results
        iocs += new_iocs

        # reformat the output
        out_dict, actual_indicator_amount = create_values_for_returned_dict(iocs, request_args)

    out_dict["last_run"] = date_to_timestamp(now)
    out_dict["current_iocs"] = iocs
    if save_integration_context:
        set_integration_context(out_dict)
    else:
        global EDL_LOCAL_CACHE
        EDL_LOCAL_CACHE = out_dict
    return out_dict[EDL_VALUES_KEY]


def find_indicators_to_limit(indicator_query: str, limit: int, offset: int = 0) -> list:
    """
    Finds indicators using demisto.searchIndicators

    Parameters:
        indicator_query (str): Query that determines which indicators to include in
            the EDL (Cortex XSOAR indicator query syntax)
        limit (int): The maximum number of indicators to include in the EDL
        offset (int): The starting index from which to fetch incidents

    Returns:
        list: The IoCs list up until the amount set by 'limit'
    """
    if offset:
        next_page = int(offset / PAGE_SIZE)

        # set the offset from the starting page
        offset_in_page = offset - (PAGE_SIZE * next_page)

    else:
        next_page = 0
        offset_in_page = 0

    # the second returned variable is the next page - it is implemented for a future use of repolling
    iocs, _ = find_indicators_to_limit_loop(indicator_query, limit, next_page=next_page)

    # if offset in page is bigger than the amount of results returned return empty list
    if len(iocs) <= offset_in_page:
        return []

    return iocs[offset_in_page:limit + offset_in_page]


def find_indicators_to_limit_loop(indicator_query: str, limit: int, total_fetched: int = 0,
                                  next_page: int = 0, last_found_len: int = None):
    """
    Finds indicators using while loop with demisto.searchIndicators, and returns result and last page

    Parameters:
        indicator_query (str): Query that determines which indicators to include in
            the EDL (Cortex XSOAR indicator query syntax)
        limit (int): The maximum number of indicators to include in the EDL
        total_fetched (int): The amount of indicators already fetched
        next_page (int): The page we are up to in the loop
        last_found_len (int): The amount of indicators found in the last fetch

    Returns:
        (tuple): The iocs and the last page
    """
    iocs: List[dict] = []
    filter_fields = "name,type"  # based on func ToIoC https://github.com/demisto/server/blob/master/domain/insight.go
    search_indicators = IndicatorsSearcher(page=next_page, filter_fields=filter_fields)
    if last_found_len is None:
        last_found_len = PAGE_SIZE
    if not last_found_len:
        last_found_len = total_fetched
    # last_found_len should be PAGE_SIZE (or PAGE_SIZE - 1, as observed for some users) for full pages
    while last_found_len in (PAGE_SIZE, PAGE_SIZE - 1) and limit and total_fetched < limit:
        fetched_iocs = search_indicators.search_indicators_by_version(query=indicator_query, size=PAGE_SIZE).get('iocs')
        # In case the result from searchIndicators includes the key `iocs` but it's value is None
        fetched_iocs = fetched_iocs or []

        # save only the value and type of each indicator
        iocs.extend({'value': ioc.get('value'), 'indicator_type': ioc.get('indicator_type')}
                    for ioc in fetched_iocs)
        last_found_len = len(fetched_iocs)
        total_fetched += last_found_len
    return iocs, search_indicators.page


def ip_groups_to_cidrs(ip_range_groups: list):
    """Collapse ip groups list to CIDRs

    Args:
        ip_range_groups (list): a list of lists containing connected IPs

    Returns:
        list. a list of CIDRs.
    """
    ip_ranges = []  # type:List
    for cidr in ip_range_groups:
        # handle single ips
        if len(cidr) == 1:
            # CIDR with a single IP appears with "/32" suffix so handle them differently
            ip_ranges.append(str(cidr[0]))
            continue

        ip_ranges.append(str(cidr))

    return ip_ranges


def ip_groups_to_ranges(ip_range_groups: list):
    """Collapse ip groups list to ranges.

    Args:
        ip_range_groups (list): a list of lists containing connected IPs

    Returns:
        list. a list of Ranges.
    """
    ip_ranges = []  # type:List
    for group in ip_range_groups:
        # handle single ips
        if len(group) == 1:
            ip_ranges.append(str(group[0]))
            continue

        ip_ranges.append(str(group))

    return ip_ranges


def ips_to_ranges(ips: list, collapse_ips: str):
    """Collapse IPs to Ranges or CIDRs.

    Args:
        ips (list): a list of IP strings.
        collapse_ips (str): Whether to collapse to Ranges or CIDRs.

    Returns:
        list. a list to Ranges or CIDRs.
    """

    if collapse_ips == COLLAPSE_TO_RANGES:
        ips_range_groups = IPSet(ips).iter_ipranges()
        return ip_groups_to_ranges(ips_range_groups)

    else:
        cidrs = IPSet(ips).iter_cidrs()
        return ip_groups_to_cidrs(cidrs)


def create_values_for_returned_dict(iocs: list, request_args: RequestArguments) -> Tuple[dict, int]:
    """
    Create a dictionary for output values
    """
    formatted_indicators = []
    ipv4_formatted_indicators = []
    ipv6_formatted_indicators = []
    for ioc in iocs:
        indicator = ioc.get('value')
        if not indicator:
            continue
        ioc_type = ioc.get('indicator_type')
        # protocol stripping
        indicator = _PROTOCOL_REMOVAL.sub('', indicator)

        if ioc_type not in [FeedIndicatorType.IP, FeedIndicatorType.IPv6,
                            FeedIndicatorType.CIDR, FeedIndicatorType.IPv6CIDR]:
            # Port stripping
            indicator_with_port = indicator
            # remove port from indicator - from demisto.com:369/rest/of/path -> demisto.com/rest/of/path
            indicator = _PORT_REMOVAL.sub(_URL_WITHOUT_PORT, indicator)
            # check if removing the port changed something about the indicator
            if indicator != indicator_with_port and not request_args.url_port_stripping:
                # if port was in the indicator and url_port_stripping param not set - ignore the indicator
                continue
            # Reformatting to PAN-OS URL format
            with_invalid_tokens_indicator = indicator
            # mix of text and wildcard in domain field handling
            indicator = _INVALID_TOKEN_REMOVAL.sub('*', indicator)
            # check if the indicator held invalid tokens
            if with_invalid_tokens_indicator != indicator:
                # invalid tokens in indicator- if drop_invalids is set - ignore the indicator
                if request_args.drop_invalids:
                    continue
            # for PAN-OS *.domain.com does not match domain.com
            # we should provide both
            # this could generate more than num entries according to PAGE_SIZE
            if indicator.startswith('*.'):
                formatted_indicators.append(indicator.lstrip('*.'))

        if request_args.collapse_ips != DONT_COLLAPSE and ioc_type == FeedIndicatorType.IP:
            ipv4_formatted_indicators.append(IPAddress(indicator))

        elif request_args.collapse_ips != DONT_COLLAPSE and ioc_type == FeedIndicatorType.IPv6:
            ipv6_formatted_indicators.append(IPAddress(indicator))

        else:
            formatted_indicators.append(indicator)

    if len(ipv4_formatted_indicators) > 0:
        ipv4_formatted_indicators = ips_to_ranges(ipv4_formatted_indicators, request_args.collapse_ips)
        formatted_indicators.extend(ipv4_formatted_indicators)

    if len(ipv6_formatted_indicators) > 0:
        ipv6_formatted_indicators = ips_to_ranges(ipv6_formatted_indicators, request_args.collapse_ips)
        formatted_indicators.extend(ipv6_formatted_indicators)
    out_dict = {
        EDL_VALUES_KEY: list_to_str(formatted_indicators, '\n'),
        "current_iocs": iocs,
        "last_limit": request_args.limit,
        "last_offset": request_args.offset,
        "drop_invalids": request_args.drop_invalids,
        "url_port_stripping": request_args.url_port_stripping,
        "collapse_ips": request_args.collapse_ips,
        "last_query": request_args.query
    }
    return out_dict, len(formatted_indicators)


def get_edl_ioc_values(on_demand: bool,
                       request_args: RequestArguments,
                       edl_cache: dict = None,
                       cache_refresh_rate: str = None) -> str:
    """
    Get the ioc list to return in the edl

    Args:
        on_demand: Whether on demand configuration is set to True or not
        request_args: the request arguments
        edl_cache: The integration context OR EDL_LOCAL_CACHE
        cache_refresh_rate: The cache_refresh_rate configuration value

    Returns:
        string representation of the iocs
    """
    if on_demand:
        # on_demand saves the EDL to integration_context
        edl_cache = get_integration_context() or {}
    elif not edl_cache:
        global EDL_LOCAL_CACHE
        edl_cache = EDL_LOCAL_CACHE or {}
    last_run = edl_cache.get('last_run')
    last_query = edl_cache.get('last_query')
    current_iocs = edl_cache.get('current_iocs')

    # on_demand ignores cache
    if on_demand:
        if request_args.is_request_change(edl_cache):
            values_str = get_ioc_values_str_from_cache(edl_cache, request_args=request_args,
                                                       iocs=current_iocs)

        else:
            values_str = get_ioc_values_str_from_cache(edl_cache, request_args=request_args)
    else:
        if last_run:
            cache_time, _ = parse_date_range(cache_refresh_rate, to_timestamp=True)
            if last_run <= cache_time or request_args.is_request_change(edl_cache) or \
                    request_args.query != last_query:
                values_str = refresh_edl_context(request_args)
            else:
                values_str = get_ioc_values_str_from_cache(edl_cache, request_args=request_args)
        else:
            values_str = refresh_edl_context(request_args)
    return values_str


def get_ioc_values_str_from_cache(edl_cache: dict,
                                  request_args: RequestArguments,
                                  iocs: list = None) -> str:
    """
    Extracts output values from cache

    Args:
        edl_cache: The integration context or EDL_LOCAL_CACHE
        request_args: The request args
        iocs: The current raw iocs data saved in the integration context
    Returns:
        string representation of the iocs
    """
    global EDL_LOCAL_CACHE
    if iocs:
        if request_args.offset > len(iocs):
            return ''

        iocs = iocs[request_args.offset: request_args.limit + request_args.offset]
        returned_dict, _ = create_values_for_returned_dict(iocs, request_args=request_args)
        EDL_LOCAL_CACHE = returned_dict

    else:
        returned_dict = edl_cache

    return returned_dict.get(EDL_VALUES_KEY, '')


def try_parse_integer(int_to_parse: Any, err_msg: str) -> int:
    """
    Tries to parse an integer, and if fails will throw DemistoException with given err_msg
    """
    try:
        res = int(int_to_parse)
    except (TypeError, ValueError):
        raise DemistoException(err_msg)
    return res


def validate_basic_authentication(headers: dict, username: str, password: str) -> bool:
    """
    Checks whether the authentication is valid.
    :param headers: The headers of the http request
    :param username: The integration's username
    :param password: The integration's password
    :return: Boolean which indicates whether the authentication is valid or not
    """
    credentials: str = headers.get('Authorization', '')
    if not credentials or 'Basic ' not in credentials:
        return False
    encoded_credentials: str = credentials.split('Basic ')[1]
    credentials: str = b64decode(encoded_credentials).decode('utf-8')
    if ':' not in credentials:
        return False
    credentials_list = credentials.split(':')
    if len(credentials_list) != 2:
        return False
    user, pwd = credentials_list
    return user == username and pwd == password


''' ROUTE FUNCTIONS '''


@APP.route('/', methods=['GET'])
def route_edl_values() -> Response:
    """
    Main handler for values saved in the integration context
    """
    params = demisto.params()

    credentials = params.get('credentials') if params.get('credentials') else {}
    username: str = credentials.get('identifier', '')
    password: str = credentials.get('password', '')
    if username and password:
        headers: dict = cast(Dict[Any, Any], request.headers)
        if not validate_basic_authentication(headers, username, password):
            err_msg: str = 'Basic authentication failed. Make sure you are using the right credentials.'
            demisto.debug(err_msg)
            return Response(err_msg, status=401)

    request_args = get_request_args(request.args, params)
    on_demand = params.get('on_demand')

    values = get_edl_ioc_values(
        on_demand=on_demand,
        request_args=request_args,
        cache_refresh_rate=params.get('cache_refresh_rate'),
    )
    return Response(values, status=200, mimetype='text/plain')


def get_request_args(request_args: dict, params: dict) -> RequestArguments:
    """
    Processing a flask request arguments and generates a RequestArguments instance from it.
    Args:
        request_args: Flask request arguments
        params: Integration configuration parameters

    Returns:
        RequestArguments instance with processed arguments
    """
    limit = try_parse_integer(request_args.get('n', params.get('edl_size', 10000)), EDL_LIMIT_ERR_MSG)
    offset = try_parse_integer(request_args.get('s', 0), EDL_OFFSET_ERR_MSG)
    query = request_args.get('q', params.get('indicators_query'))
    strip_port = request_args.get('sp', params.get('url_port_stripping', False))
    drop_invalids = request_args.get('di', params.get('drop_invalids', False))
    collapse_ips = request_args.get('tr', params.get('collapse_ips', DONT_COLLAPSE))

    # handle flags
    if drop_invalids == '':
        drop_invalids = True

    if strip_port == '':
        strip_port = True

    if collapse_ips not in [DONT_COLLAPSE, COLLAPSE_TO_CIDR, COLLAPSE_TO_RANGES]:
        collapse_ips = try_parse_integer(collapse_ips, EDL_COLLAPSE_ERR_MSG)

        if collapse_ips not in [0, 1, 2]:
            raise DemistoException(EDL_COLLAPSE_ERR_MSG)

        collapse_options = {
            0: DONT_COLLAPSE,
            1: COLLAPSE_TO_RANGES,
            2: COLLAPSE_TO_CIDR
        }
        collapse_ips = collapse_options[collapse_ips]
    return RequestArguments(query, limit, offset, strip_port, drop_invalids, collapse_ips)


''' COMMAND FUNCTIONS '''


def test_module(_: Dict, params: Dict):
    """
    Validates:
        1. Valid port.
        2. Valid cache_refresh_rate
    """
    get_params_port(params)
    on_demand = params.get('on_demand', None)
    if not on_demand:
        try_parse_integer(params.get('edl_size'), EDL_LIMIT_ERR_MSG)  # validate EDL Size was set
        query = params.get('indicators_query')  # validate indicators_query isn't empty
        if not query:
            raise ValueError('"Indicator Query" is required. Provide a valid query.')
        cache_refresh_rate = params.get('cache_refresh_rate', '')
        if not cache_refresh_rate:
            raise ValueError(EDL_MISSING_REFRESH_ERR_MSG)
        # validate cache_refresh_rate value
        range_split = cache_refresh_rate.split(' ')
        if len(range_split) != 2:
            raise ValueError(EDL_MISSING_REFRESH_ERR_MSG)
        try_parse_integer(range_split[0], 'Invalid time value for the Refresh Rate. Must be a valid integer.')
        if not range_split[1] in ['minute', 'minutes', 'hour', 'hours', 'day', 'days', 'month', 'months', 'year',
                                  'years']:
            raise ValueError(
                'Invalid time unit for the Refresh Rate. Must be minutes, hours, days, months, or years.')
        parse_date_range(cache_refresh_rate, to_timestamp=True)
    run_long_running(params, is_test=True)
    return 'ok', {}, {}


def run_long_running(params: Dict, is_test: bool = False):
    """
    Start the long running server
    :param params: Demisto params
    :param is_test: Indicates whether it's test-module run or regular run
    :return: None
    """
    certificate: str = params.get('certificate', '')
    private_key: str = params.get('key', '')

    certificate_path = str()
    private_key_path = str()

    try:
        port = get_params_port(params)
        ssl_args = dict()

        if (certificate and not private_key) or (private_key and not certificate):
            raise DemistoException('If using HTTPS connection, both certificate and private key should be provided.')

        if certificate and private_key:
            certificate_file = NamedTemporaryFile(delete=False)
            certificate_path = certificate_file.name
            certificate_file.write(bytes(certificate, 'utf-8'))
            certificate_file.close()

            private_key_file = NamedTemporaryFile(delete=False)
            private_key_path = private_key_file.name
            private_key_file.write(bytes(private_key, 'utf-8'))
            private_key_file.close()
            context = SSLContext(PROTOCOL_TLSv1_2)
            context.load_cert_chain(certificate_path, private_key_path)
            ssl_args['ssl_context'] = context
            demisto.debug('Starting HTTPS Server')
        else:
            demisto.debug('Starting HTTP Server')

        server = WSGIServer(('0.0.0.0', port), APP, **ssl_args, log=DEMISTO_LOGGER, error_log=ERROR_LOGGER)
        if is_test:
            server_process = Process(target=server.serve_forever)
            server_process.start()
            time.sleep(5)
            server_process.terminate()
        else:
            server.serve_forever()
    except SSLError as e:
        ssl_err_message = f'Failed to validate certificate and/or private key: {str(e)}'
        demisto.error(ssl_err_message)
        raise ValueError(ssl_err_message)
    except Exception as e:
        demisto.error(f'An error occurred in long running loop: {str(e)}')
        raise ValueError(str(e))
    finally:
        if certificate_path:
            os.unlink(certificate_path)
        if private_key_path:
            os.unlink(private_key_path)


def update_edl_command(args: Dict, params: Dict):
    """
    Updates the EDL values and format on demand
    """
    on_demand = params.get('on_demand')
    if not on_demand:
        raise DemistoException(
            '"Update EDL On Demand" is off. If you want to update the EDL manually please toggle it on.')
    limit = try_parse_integer(args.get('edl_size', params.get('edl_size')), EDL_LIMIT_ERR_MSG)
    print_indicators = args.get('print_indicators')
    query = args.get('query', '')
    collapse_ips = args.get('collapse_ips', DONT_COLLAPSE)
    url_port_stripping = args.get('url_port_stripping', '').lower() == 'true'
    drop_invalids = args.get('drop_invalids', '').lower() == 'true'
    offset = try_parse_integer(args.get('offset', 0), EDL_OFFSET_ERR_MSG)
    request_args = RequestArguments(query, limit, offset, url_port_stripping, drop_invalids, collapse_ips)
    indicators = refresh_edl_context(request_args, save_integration_context=True)
    hr = tableToMarkdown('EDL was updated successfully with the following values', indicators,
                         ['Indicators']) if print_indicators == 'true' else 'EDL was updated successfully'
    return hr, {}, indicators


def main():
    """
    Main
    """
    global PAGE_SIZE
    params = demisto.params()
    try:
        PAGE_SIZE = max(1, int(params.get('page_size') or PAGE_SIZE))
    except ValueError:
        demisto.debug(f'Non integer "page_size" provided: {params.get("page_size")}. defaulting to {PAGE_SIZE}')
    credentials = params.get('credentials') if params.get('credentials') else {}
    username: str = credentials.get('identifier', '')
    password: str = credentials.get('password', '')
    if (username and not password) or (password and not username):
        err_msg: str = 'If using credentials, both username and password should be provided.'
        demisto.debug(err_msg)
        raise DemistoException(err_msg)

    command = demisto.command()
    demisto.debug(f'Command being called is {command}')
    commands = {
        'test-module': test_module,
        'edl-update': update_edl_command,
    }

    try:
        if command == 'long-running-execution':
            run_long_running(params)
        elif command in commands:
            readable_output, outputs, raw_response = commands[command](demisto.args(), params)
            return_outputs(readable_output, outputs, raw_response)
        else:
            raise NotImplementedError(f'Command "{command}" is not implemented.')
    except Exception as e:
        err_msg = f'Error in {INTEGRATION_NAME} Integration [{e}]'
        return_error(err_msg)


if __name__ in ['__main__', '__builtin__', 'builtins']:
    main()
