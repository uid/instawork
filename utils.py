import cgi
import urllib
import urlparse

def url_with_params(url, params):
    url_parts = list(urlparse.urlparse(url))
    query = dict(cgi.parse_qsl(url_parts[4])) # XXX moved to urlparse in Python 2.6
    query.update(params)
    url_parts[4] = urllib.urlencode(query)
    return urlparse.urlunparse(url_parts)
