#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 2012-2015 Matt Martz
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os
import re
import sys
import math
import signal
import socket
import timeit
import platform
import threading
import logging
import queue
import urllib.parse


__version__ = '0.3.4'

# Some global variables we use
user_agent = None
source = None
shutdown_event = None
scheme = 'http'

# Used for bound_interface
socket_socket = socket.socket


try:
    import xml.etree.cElementTree as ET
except ImportError:
    try:
        import xml.etree.ElementTree as ET
    except ImportError:
        from xml.dom import minidom as DOM

        ET = None


try:
    from urllib2 import urlopen, Request, HTTPError, URLError
except ImportError:
    from urllib.request import urlopen, Request, HTTPError, URLError

try:
    from httplib import HTTPConnection, HTTPSConnection
except ImportError:
    e_http_py2 = sys.exc_info()
    try:
        from http.client import HTTPConnection, HTTPSConnection
    except ImportError:
        e_http_py3 = sys.exc_info()
        raise SystemExit('Your python installation is missing required HTTP '
                         'client classes:\n\n'
                         'Python 2: %s\n'
                         'Python 3: %s' % (e_http_py2[1], e_http_py3[1]))


class SpeedtestDataError(Exception):
    """Exception class used to indicate an invalid data returned from Speedtest server

    """


class SpeedtestServerListError(Exception):
    """Internal Exception class used to indicate to move on to the next
    URL for retrieving speedtest.net server details

    """


class SpeedtestServerInvalidServerIDError(Exception):
    """Exception class used to indicate that a given server ID is invalid

    """


def bound_socket(*args, **kwargs):
    """Bind socket to a specified source IP address"""

    global source
    sock = socket_socket(*args, **kwargs)
    sock.bind((source, 0))
    return sock


def distance(origin, destination):
    """Determine distance between 2 sets of [lat,lon] in km"""

    lat1, lon1 = origin
    lat2, lon2 = destination
    radius = 6371  # km

    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) * math.sin(dlat / 2) +
         math.cos(math.radians(lat1)) *
         math.cos(math.radians(lat2)) * math.sin(dlon / 2) *
         math.sin(dlon / 2))
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    d = radius * c

    return d


def build_user_agent():
    """Build a Mozilla/5.0 compatible User-Agent string"""

    global user_agent
    if user_agent:
        return user_agent

    ua_tuple = (
        'Mozilla/5.0',
        '(%s; U; %s; en-us)' % (platform.system(), platform.architecture()[0]),
        'Python/%s' % platform.python_version(),
        '(KHTML, like Gecko)',
        'speedtest-cli/%s' % __version__
    )
    user_agent = ' '.join(ua_tuple)
    return user_agent


def build_request(url, data=None, headers={}):
    """Build a urllib2 request object

    This function automatically adds a User-Agent header to all requests

    """

    if url[0] == ':':
        schemed_url = '%s%s' % (scheme, url)
    else:
        schemed_url = url

    headers['User-Agent'] = user_agent
    return Request(schemed_url, data=data, headers=headers)


def catch_request(request):
    """Helper function to catch common exceptions encountered when
    establishing a connection with a HTTP/HTTPS request

    """

    try:
        uh = urlopen(request)
        return uh, False
    except (HTTPError, URLError, socket.error):
        e = sys.exc_info()[1]
        return None, e


class FileGetter(threading.Thread):
    """Thread class for retrieving a URL"""

    def __init__(self, url, start):
        self.url = url
        self.result = None
        self.starttime = start
        threading.Thread.__init__(self)

    def run(self):
        self.result = [0]
        try:
            if (timeit.default_timer() - self.starttime) <= 10:
                request = build_request(self.url)
                f = urlopen(request)
                while 1 and not shutdown_event.isSet():
                    self.result.append(len(f.read(10240)))
                    if self.result[-1] == 0:
                        break
                f.close()
        except IOError:
            pass


def downloadSpeed(files, quiet=False):
    """Function to launch FileGetter threads and calculate download speeds"""

    start = timeit.default_timer()

    def producer(q, files):
        for file in files:
            thread = FileGetter(file, start)
            thread.start()
            q.put(thread, True)
            if not quiet and not shutdown_event.isSet():
                sys.stdout.write('.')
                sys.stdout.flush()

    finished = []

    def consumer(q, total_files):
        while len(finished) < total_files:
            thread = q.get(True)
            while thread.isAlive():
                thread.join(timeout=0.1)
            finished.append(sum(thread.result))
            del thread

    q = queue.Queue(6)
    prod_thread = threading.Thread(target=producer, args=(q, files))
    cons_thread = threading.Thread(target=consumer, args=(q, len(files)))
    start = timeit.default_timer()
    prod_thread.start()
    cons_thread.start()
    while prod_thread.isAlive():
        prod_thread.join(timeout=0.1)
    while cons_thread.isAlive():
        cons_thread.join(timeout=0.1)
    return (sum(finished) / (timeit.default_timer() - start))


class FilePutter(threading.Thread):
    """Thread class for putting a URL"""

    def __init__(self, url, start, size):
        self.url = url
        chars = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        data = chars * (int(round(int(size) / 36.0)))
        self.data = ('content1=%s' % data[0:int(size) - 9]).encode()
        del data
        self.result = None
        self.starttime = start
        threading.Thread.__init__(self)

    def run(self):
        try:
            if ((timeit.default_timer() - self.starttime) <= 10 and
                    not shutdown_event.isSet()):
                request = build_request(self.url, data=self.data)
                f = urlopen(request)
                f.read(11)
                f.close()
                self.result = len(self.data)
            else:
                self.result = 0
        except IOError:
            self.result = 0


def uploadSpeed(url, sizes, quiet=False):
    """Function to launch FilePutter threads and calculate upload speeds"""

    start = timeit.default_timer()

    def producer(q, sizes):
        for size in sizes:
            thread = FilePutter(url, start, size)
            thread.start()
            q.put(thread, True)
            if not quiet and not shutdown_event.isSet():
                sys.stdout.write('.')
                sys.stdout.flush()

    finished = []

    def consumer(q, total_sizes):
        while len(finished) < total_sizes:
            thread = q.get(True)
            while thread.isAlive():
                thread.join(timeout=0.1)
            finished.append(thread.result)
            del thread

    q = queue.Queue(6)
    prod_thread = threading.Thread(target=producer, args=(q, sizes))
    cons_thread = threading.Thread(target=consumer, args=(q, len(sizes)))
    start = timeit.default_timer()
    prod_thread.start()
    cons_thread.start()
    while prod_thread.isAlive():
        prod_thread.join(timeout=0.1)
    while cons_thread.isAlive():
        cons_thread.join(timeout=0.1)
    return (sum(finished) / (timeit.default_timer() - start))


def getAttributesByTagName(dom, tagName):
    """Retrieve an attribute from an XML document and return it in a
    consistent format

    Only used with xml.dom.minidom, which is likely only to be used
    with python versions older than 2.5
    """
    elem = dom.getElementsByTagName(tagName)[0]
    return dict(list(elem.attributes.items()))


def getConfig():
    """Download the speedtest.net configuration and return only the data
    we are interested in
    """

    request = build_request('://www.speedtest.net/speedtest-config.php')
    uh, e = catch_request(request)
    if e:
        logging.error('Could not retrieve speedtest.net configuration: %s' % e)
        raise SpeedtestDataError
    configxml = []
    while 1:
        configxml.append(uh.read(10240))
        if len(configxml[-1]) == 0:
            break
    if int(uh.code) != 200:
        return None
    uh.close()
    try:
        try:
            root = ET.fromstring(''.encode().join(configxml))
            config = {
                'client': root.find('client').attrib,
                'times': root.find('times').attrib,
                'download': root.find('download').attrib,
                'upload': root.find('upload').attrib}
        except AttributeError:  # Python3 branch
            root = DOM.parseString(''.join(configxml))
            config = {
                'client': getAttributesByTagName(root, 'client'),
                'times': getAttributesByTagName(root, 'times'),
                'download': getAttributesByTagName(root, 'download'),
                'upload': getAttributesByTagName(root, 'upload')}
    except SyntaxError:
        logging.error('Failed to parse speedtest.net configuration')
        raise SpeedtestDataError
    del root
    del configxml
    return config


def closest_servers(client, number=10):
    """Determine the 5 closest speedtest.net servers based on geographic
    distance

    use number==0 to list all servers

    """

    urls = [
        '://www.speedtest.net/speedtest-servers-static.php',
        '://c.speedtest.net/speedtest-servers-static.php',
        '://www.speedtest.net/speedtest-servers.php',
        '://c.speedtest.net/speedtest-servers.php',
    ]
    errors = []
    servers = {}
    for url in urls:
        try:
            request = build_request(url)
            uh, e = catch_request(request)
            if e:
                errors.append('%s' % e)
                raise SpeedtestServerListError
            serversxml = []
            while 1:
                serversxml.append(uh.read(10240))
                if len(serversxml[-1]) == 0:
                    break
            if int(uh.code) != 200:
                uh.close()
                raise SpeedtestServerListError
            uh.close()
            try:
                try:
                    root = ET.fromstring(''.encode().join(serversxml))
                    elements = root.getiterator('server')
                except AttributeError:  # Python3 branch
                    root = DOM.parseString(''.join(serversxml))
                    elements = root.getElementsByTagName('server')
            except SyntaxError:
                raise SpeedtestServerListError
            for server in elements:
                try:
                    attrib = server.attrib
                except AttributeError:
                    attrib = dict(list(server.attributes.items()))
                d = distance([float(client['lat']),
                              float(client['lon'])],
                             [float(attrib.get('lat')),
                              float(attrib.get('lon'))])
                attrib['d'] = d
                if d not in servers:
                    servers[d] = [attrib]
                else:
                    servers[d].append(attrib)
            del root
            del serversxml
            del elements
        except SpeedtestServerListError:
            continue

        # We were able to fetch and parse the list of speedtest.net servers
        if servers:
            break

    if not servers:
        logging.error('Failed to retrieve list of speedtest.net servers:\n\n %s' % '\n'.join(errors))
        raise SpeedtestDataError()

    closest = []
    for d in sorted(servers.keys()):
        for s in servers[d]:
            closest.append(s)
            if len(closest) == number:
                break
        else:
            continue
        break

    del servers
    return closest


def getBestServer(servers):
    """Perform a speedtest.net latency request to determine which
    speedtest.net server has the lowest latency
    """

    results = {}
    for server in servers:
        cum = []
        url = '%s/latency.txt' % os.path.dirname(server['url'])
        urlparts = urllib.parse.urlparse(url)
        for i in range(0, 3):
            try:
                if urlparts[0] == 'https':
                    h = HTTPSConnection(urlparts[1])
                else:
                    h = HTTPConnection(urlparts[1])
                headers = {'User-Agent': user_agent}
                start = timeit.default_timer()
                h.request("GET", urlparts[2], headers=headers)
                r = h.getresponse()
                total = (timeit.default_timer() - start)
            except (HTTPError, URLError, socket.error):
                cum.append(3600)
                continue
            text = r.read(9)
            if int(r.status) == 200 and text == 'test=test'.encode():
                cum.append(total)
            else:
                cum.append(3600)
            h.close()
        avg = round((sum(cum) / 6) * 1000, 3)
        results[avg] = server
    fastest = sorted(results.keys())[0]
    best = results[fastest]
    best['latency'] = fastest

    return best


def cancel_test():
    global shutdown_event
    shutdown_event.set()


def test_speed(timeout=100, secure=True, server=None):
    global shutdown_event, source, scheme
    shutdown_event = threading.Event()

    socket.setdefaulttimeout(timeout)
    build_user_agent()

    if secure:
        scheme = 'https'

    config = getConfig()

    if server:
        servers = closest_servers(config['client'], 0)
    else:
        servers = closest_servers(config['client'])

    if server:
        try:
            best = getBestServer(filter(lambda x: x['id'] == server, servers))
        except IndexError as e:
            raise SpeedtestServerInvalidServerIDError(e)
    else:
        logging.info('Selecting best server based on latency')
        best = getBestServer(servers)

    ping = best['latency']
    logging.info(('Hosted by %(sponsor)s (%(name)s) [%(d)0.2f km]: ''%(latency)s ms' % best).encode('utf-8', 'ignore'))

    sizes = [350, 500, 750, 1000, 1500, 2000, 2500, 3000, 3500, 4000]
    urls = []
    for size in sizes:
        for i in range(0, 4):
            urls.append('%s/random%sx%s.jpg' % (os.path.dirname(best['url']), size, size))
    dlspeed = downloadSpeed(urls, True)
    logging.info('Download: %0.2f Mbit/s' % ((dlspeed / 1000 / 1000) * 8))

    sizesizes = [int(.25 * 1000 * 1000), int(.5 * 1000 * 1000)]
    sizes = []
    for size in sizesizes:
        for i in range(0, 25):
            sizes.append(size)
    ulspeed = uploadSpeed(best['url'], sizes, True)
    logging.info('Upload: %0.2f Mbit/s' % ((ulspeed / 1000 / 1000) * 8))

    return ping, dlspeed, ulspeed, best


def list_servers():
    build_user_agent()
    config = getConfig()

    return closest_servers(config['client'], 0)


def main():
    try:
        servers = list_servers()
        for server in servers:
            print('%(id)4s) %(sponsor)s (%(name)s, %(country)s) ''[%(d)0.2f km]' % server)

        result = test_speed(timeout=30, secure=True)
        print(result)
    except KeyboardInterrupt:
        cancel_test()
        print('\nCancelling...')


if __name__ == '__main__':
    main()

# vim:ts=4:sw=4:expandtab
