'''
    flaskext.seasurf
    ----------------

    A Flask extension providing fairly good protection against cross-site
    request forgery (CSRF), otherwise known as "sea surf".

    :copyright: (c) 2011 by Max Countryman.
    :license: BSD, see LICENSE for more details.
'''

from __future__ import absolute_import

__version_info__ = ('1', '1', '1')
__version__ = '.'.join(__version_info__)
__author__ = 'Max Countryman'
__license__ = 'BSD'
__copyright__ = '(c) 2011 by Max Countryman'
__all__ = ['SeaSurf']

import calendar
import hashlib
import random

from datetime import datetime, timedelta
import urllib.parse as urlparse

from flask import (_app_ctx_stack, current_app, g, has_request_context, request,
                   session)
from werkzeug.exceptions import BadRequest, Forbidden

try:
    from hmac import compare_digest as safe_str_cmp
except ImportError:
    from werkzeug.security import safe_str_cmp


_MAX_CSRF_KEY = 2 << 63


if hasattr(random, 'SystemRandom'):
    random = random.SystemRandom()
else:
    random = random

try:
    import secrets
except ImportError:
    secrets = None

REASON_NO_REFERER = u'Referer checking failed: no referer.'
REASON_BAD_REFERER = u'Referer checking failed: {0} does not match {1}.'
REASON_NO_CSRF_TOKEN = u'CSRF token not set.'
REASON_BAD_TOKEN = u'CSRF token missing or incorrect.'
REASON_NO_REQUEST = u'CSRF validation can only happen within a request context.'


def _same_origin(url1, url2):
    '''
    Determine if two URLs share the same origin.

    :param url1: The first URL to compare.
    :param url2: The second URL to compare.
    '''
    try:
        p1, p2 = urlparse.urlparse(url1), urlparse.urlparse(url2)
        origin1 = p1.scheme, p1.hostname, p1.port
        origin2 = p2.scheme, p2.hostname, p2.port
        return origin1 == origin2
    except ValueError:
        return False


class SeaSurf(object):
    '''
    Primary class container for CSRF validation logic. The main function of
    this extension is to generate and validate CSRF tokens. The design and
    implementation of this extension is influenced by Django's CSRF middleware.

    Tokens are generated using a salted SHA1 hash. The salt is based off
    a random range. The OS's SystemRandom is used if available, otherwise
    the core random.randrange is used.

    You might intialize :class:`SeaSurf` something like this::

        csrf = SeaSurf()

    Then pass the application object to be configured::

        csrf.init_app(app)

    Validation will now be active for all requests whose methods are not GET,
    HEAD, OPTIONS, or TRACE.

    When using other request methods, such as POST for instance, you will need
    to provide the CSRF token as a parameter. This can be achieved by making
    use of the Jinja global. In your template::

        <form method="POST">
        ...
        <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
        </form>

    This will assign a token to both the session cookie and the rendered HTML
    which will then be validated on the backend. POST requests missing this
    field will fail unless the header X-CSRFToken is specified.

    .. admonition:: Excluding Views From Validation

        For views that use methods which may be validated but for which you
        wish to not run validation on you may make use of the :class:`exempt`
        decorator to indicate that they should not be checked.
    '''

    def __init__(self, app=None):
        self._exempt_views = set()
        self._include_views = set()
        self._set_cookie_views = set()
        self._exempt_urls = tuple()
        self._disable_cookie = None
        self._skip_validation = None

        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        '''
        Initializes a Flask object `app`, binds CSRF validation to
        app.before_request, and assigns `csrf_token` as a Jinja global.

        :param app: The Flask application object.
        '''

        app.before_request(self._before_request)
        app.after_request(self._after_request)

        # Expose the CSRF token to the template.
        app.jinja_env.globals['csrf_token'] = self._get_token

        self._csrf_name = app.config.get('CSRF_COOKIE_NAME', '_csrf_token')
        self._csrf_header_name = app.config.get('CSRF_HEADER_NAME',
                                                'X-CSRFToken')
        self._csrf_disable = app.config.get('CSRF_DISABLE',
                                            app.config.get('TESTING', False))
        self._csrf_timeout = app.config.get('CSRF_COOKIE_TIMEOUT',
                                            timedelta(days=5))
        self._csrf_secure = app.config.get('CSRF_COOKIE_SECURE', False)
        self._csrf_httponly = app.config.get('CSRF_COOKIE_HTTPONLY', False)
        self._csrf_path = app.config.get('CSRF_COOKIE_PATH', '/')
        self._csrf_domain = app.config.get('CSRF_COOKIE_DOMAIN')
        self._csrf_samesite = app.config.get('CSRF_COOKIE_SAMESITE', 'Lax')
        self._check_referer = app.config.get('CSRF_CHECK_REFERER', True)
        self._type = app.config.get('SEASURF_INCLUDE_OR_EXEMPT_VIEWS',
                                    'exempt')

        return self

    def exempt(self, view):
        '''
        A decorator that can be used to exclude a view from CSRF validation.

        Example usage of :class:`exempt` might look something like this::

            csrf = SeaSurf(app)

            @csrf.exempt
            @app.route('/insecure')
            def insecure():
                return render_template('insecure.html')

        :param view: The view to be wrapped by the decorator.
        '''

        view_location = '{0}.{1}'.format(view.__module__, view.__name__)
        self._exempt_views.add(view_location)
        return view

    def exempt_urls(self, urls):
        self._exempt_urls = urls

    def include(self, view):
        '''
        A decorator that can be used to include a view in CSRF validation when
        `SEASURF_INCLUDE_OR_EXEMPT_VIEWS` is set to `"include"`.

        Example usage of :class:`include` might look something like this::

            csrf = SeaSurf(app)

            @csrf.include
            @app.route('/some_view')
            def some_view():
                return render_template('some_view.html')

        :param view: The view to be wrapped by the decorator.
        '''

        view_location = '{0}.{1}'.format(view.__module__, view.__name__)
        self._include_views.add(view_location)
        return view

    def disable_cookie(self, callback):
        '''
        A decorator to programmatically disable setting the CSRF token cookie
        on the response. The function will be passed a Flask Response object
        for the current request.

        The decorated function must return :class:`True` or :class:`False`.

        Example usage of :class:`disable_cookie` might look something
        like::

            csrf = SeaSurf(app)

            @csrf.disable_cookie
            def disable_cookie(response):
                if is_api_request():
                    return True
                return False
        '''

        self._disable_cookie = callback
        return callback

    def set_cookie(self, view):
        '''
        A decorator that can be used to force setting the CSRF token cookie on
        the request. By default, the CSRF token cookie is set on all requests
        unless the view is decorated with :class:`exempt`. This decorator is a
        noop unless used in conjuction with :class:`exempt`.

        Example usage of :class:`set_cookie` might look something like this::

            csrf = SeaSurf(app)

            @csrf.exempt
            @csrf.set_cookie
            @app.route('/some_view')
            def some_view():
                return render_template('some_view.html')

        :param view: The view to be wrapped by the decorator.
        '''

        view_location = '{0}.{1}'.format(view.__module__, view.__name__)
        self._set_cookie_views.add(view_location)
        return view

    def skip_validation(self, callback):
        '''
        A decorator to programmatically disable validating the CSRF token
        cookie on the request. The function will be passed a Flask Request
        object for the current request.

        The decorated function must return :class:`True` or :class:`False`.

        Example usage of :class:`skip_validation` might look something
        like::

            csrf = SeaSurf(app)

            @csrf.skip_validation
            def skip_validation(request):
                if not_using_cookie_authorization():
                    return True
                return False
        '''

        self._skip_validation = callback
        return callback

    def validate(self):
        '''
        Validates a CSRF token for the current request.

        If CSRF token is invalid, stops execution and sends a Forbidden error
        response to the client. Can be used in combination with :class:`exempt`
        to programmatically enable CSRF protection per request.

        Example usage of :class:`validate` might look something
        like::

            csrf = SeaSurf(app)

            @csrf.exempt
            @app.route('/sometimes_requires_csrf')
            def sometimes_requires_csrf():
                if not oauth_request():
                    # validate csrf unless this is an OAuth request
                    csrf.validate()
                return render_template('sometimes_requires_csrf.html')
        '''

        if not has_request_context():
            raise Forbidden(description=REASON_NO_REQUEST)

        # Tell _after_request to still set the CSRF token cookie when this
        # view was exemp, but validate was called manually in the view
        g.csrf_validation_checked = True

        server_csrf_token = session.get(self._csrf_name, None)

        if request.is_secure and self._check_referer:
            referer = request.headers.get('Referer')
            if referer is None:
                error = (REASON_NO_REFERER, request.path)
                error = u'Forbidden ({0}): {1}'.format(*error)
                current_app.logger.warning(error)
                raise Forbidden(description=REASON_NO_REFERER)

            # By setting the Access-Control-Allow-Origin header, browsers
            # will let you send cross-domain AJAX requests so if there is
            # an Origin header, the browser has already decided that it
            # trusts this domain otherwise it would have blocked the
            # request before it got here.
            allowed_referer = request.headers.get('Origin') or request.url_root
            if not _same_origin(referer, allowed_referer):
                error = REASON_BAD_REFERER.format(referer, allowed_referer)
                description = error
                error = (error, request.path)
                error = u'Forbidden ({0}): {1}'.format(*error)
                current_app.logger.warning(error)
                raise Forbidden(description=description)

        request_csrf_token = request.form.get(self._csrf_name, '')
        if not request_csrf_token:
            # Check to see if the data is being sent as JSON
            try:
                if hasattr(request, 'is_json') and request.is_json \
                  and hasattr(request, 'json') and request.json:
                    request_csrf_token = request.json.get(self._csrf_name, '')

            # Except Attribute error if JSON data is a list
            except (BadRequest, AttributeError):
                pass

        if not request_csrf_token:
            # As per the Django middleware, this makes AJAX easier and
            # PUT and DELETE possible.
            request_csrf_token = request.headers.get(self._csrf_header_name, '')

        some_none = None in (request_csrf_token, server_csrf_token)
        if some_none or not safe_str_cmp(request_csrf_token, server_csrf_token):
            error = (REASON_BAD_TOKEN, request.path)
            error = u'Forbidden ({0}): {1}'.format(*error)
            current_app.logger.warning(error)
            raise Forbidden(description=REASON_BAD_TOKEN)

    def generate_new_token(self):
        '''
        Delete current CSRF token and generate a new CSRF token.  This function
        should only be called inside a view function to avoid conflicts with
        other operations that this library performs during the request context
        '''
        new_csrf_token = self._generate_token()

        session[self._csrf_name] = new_csrf_token
        setattr(_app_ctx_stack.top, self._csrf_name, new_csrf_token)

    def _should_use_token(self, view_func):
        '''
        Given a view function, determine whether or not we should validate CSRF
        tokens upon requests to this view.

        :param view_func: A view function.
        '''
        if (hasattr(g, 'csrf_validation_checked') and
            getattr(g, 'csrf_validation_checked')):
            return True

        if view_func is None or self._type not in ('exempt', 'include'):
            return False

        view = '{0}.{1}'.format(view_func.__module__, view_func.__name__)
        if self._type == 'exempt' and view in self._exempt_views:
            return False

        if self._type == 'include' and view not in self._include_views:
            return False

        url = u'{0}{1}'.format(request.script_root, request.path)
        if url.startswith(self._exempt_urls):
            return False

        return True

    def _should_set_cookie(self, view_func):
        '''
        Given a view function, determine whether or not we should deliver a
        CSRF token to this view through the response.

        :param view_func: A view function.
        '''

        if self._should_use_token(view_func):
            return True

        view = '{0}.{1}'.format(view_func.__module__, view_func.__name__)
        if view in self._set_cookie_views:
            return True

        return False

    def _before_request(self):
        '''
        Determine if a view is exempt from CSRF validation and if not
        then ensure the validity of the CSRF token. This method is bound to
        the Flask `before_request` decorator.

        If a request is determined to be secure, i.e. using HTTPS, then we
        use strict referer checking to prevent a man-in-the-middle attack
        from being plausible.

        Validation is suspended if `TESTING` is True in your application's
        configuration.
        '''

        if self._csrf_disable:
            return  # don't validate for testing

        server_csrf_token = session.get(self._csrf_name, None)
        if not server_csrf_token:
            setattr(_app_ctx_stack.top,
                    self._csrf_name,
                    self._generate_token())
        else:
            setattr(_app_ctx_stack.top, self._csrf_name, server_csrf_token)

        # Always set this to let the response know whether or not to set the
        # CSRF token.
        _app_ctx_stack.top._view_func = \
            current_app.view_functions.get(request.endpoint)

        if request.method not in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
            # Retrieve the view function based on the request endpoint and
            # then compare it to the set of exempted views
            if not self._should_use_token(_app_ctx_stack.top._view_func):
                return

            if self._skip_validation and self._skip_validation(request):
                return

            self.validate()

    def _after_request(self, response):
        '''
        Checks if the `flask._app_ctx_object` object contains the CSRF token,
        and if the view in question has CSRF protection enabled. If both,
        goes on to check if a cookie needs to be set by verifying the cookie
        presented by the request matches the CSRF token and the user has not
        requested a token in a Jinja template.

        If the token does not match or the user has requested a token,returns
        the response with a cookie containing the token. Otherwise we return
        the response unaltered. Bound to the Flask `after_request` decorator.

        :param response: A Flask Response object.
        '''
        if getattr(_app_ctx_stack.top, self._csrf_name, None) is None:
            return response

        _view_func = getattr(_app_ctx_stack.top, '_view_func', False)
        if not (_view_func and self._should_set_cookie(_view_func)):
            return response

        # Don't apply set_cookie if the request included the cookie
        # and did not request a token (ie simple AJAX requests, etc)
        csrf_cookie_matches = request.cookies.get(self._csrf_name, False) == getattr(_app_ctx_stack.top, self._csrf_name)
        if csrf_cookie_matches and not getattr(_app_ctx_stack.top, 'csrf_token_requested', False):
            return response

        if self._disable_cookie and self._disable_cookie(response):
            return response

        self._set_csrf_cookie(response)
        return response

    def _set_csrf_cookie(self, response):
        '''
        Adds a csrf token cookie to the given response.

        :param response: A Flask Response object.
        '''

        csrf_token = getattr(_app_ctx_stack.top, self._csrf_name)
        if session.get(self._csrf_name) != csrf_token:
            session[self._csrf_name] = csrf_token
        expires_at = datetime.utcnow() + self._csrf_timeout
        response.set_cookie(self._csrf_name,
                            csrf_token,
                            max_age=self._csrf_timeout,
                            expires=calendar.timegm(expires_at.utctimetuple()),
                            secure=self._csrf_secure,
                            httponly=self._csrf_httponly,
                            path=self._csrf_path,
                            domain=self._csrf_domain,
                            samesite=self._csrf_samesite)
        response.vary.add('Cookie')

    def _get_token(self):
        '''
        Attempts to get a token from the request cookies.

        This is passed to the Jinja env globals as 'csrf_token'.
        '''
        # The Django behavior is to flag any call to `get_token` so that the middleware
        # will only pass the Set-Cookie header when a request needs a token generated
        # generated or has requested one in it's template.
        # See https://github.com/django/django/blob/86de930f/django/middleware/csrf.py#L74
        _app_ctx_stack.top.csrf_token_requested = True

        token = getattr(_app_ctx_stack.top, self._csrf_name, None)
        if isinstance(token, bytes):
            return token.decode('utf8')
        return token

    def _generate_token(self):
        '''
        Generates a token using PEP 506 secrets module (if available), or falling back to a SHA1-salted random.
        '''
        if secrets:
            # use PEP 506 secrets module
            return secrets.token_hex()
        else:
            salt = str(random.randrange(0, _MAX_CSRF_KEY)).encode('utf-8')
            return hashlib.sha1(salt).hexdigest()
