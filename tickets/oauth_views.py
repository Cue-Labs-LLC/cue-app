"""
OAuth 2.0 authorization server for Cue MCP.

Implements authorization_code + PKCE/S256 per RFC 6749, RFC 7591 (dynamic client
registration), RFC 7009 (token revocation), and RFC 8414 (server metadata).

This is what lets Claude Desktop's Connectors page authenticate against /mcp.
"""
import base64
import hashlib
import json
import logging
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import OAuthAccessToken, OAuthAuthorizationCode, OAuthClient
from .utils import get_organization

logger = logging.getLogger(__name__)


def _site_url():
    return getattr(settings, 'SITE_URL', 'http://localhost:8000').rstrip('/')


def _err(code, description=None, status=400):
    body = {'error': code}
    if description:
        body['error_description'] = description
    return JsonResponse(body, status=status)


# ---------------------------------------------------------------------------
# Discovery endpoints
# ---------------------------------------------------------------------------

def oauth_server_metadata(request):
    """GET /.well-known/oauth-authorization-server — RFC 8414."""
    base = _site_url()
    return JsonResponse({
        'issuer': base,
        'authorization_endpoint': f'{base}/oauth/authorize/',
        'token_endpoint': f'{base}/oauth/token/',
        'registration_endpoint': f'{base}/oauth/clients/register/',
        'revocation_endpoint': f'{base}/oauth/revoke/',
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code'],
        'code_challenge_methods_supported': ['S256'],
        'token_endpoint_auth_methods_supported': ['none'],
        'scopes_supported': ['mcp'],
    })


def oauth_protected_resource_metadata(request):
    """GET /.well-known/oauth-protected-resource — RFC 9728 / MCP 2025."""
    base = _site_url()
    return JsonResponse({
        'resource': f'{base}/mcp',
        'authorization_servers': [base],
        'bearer_methods_supported': ['header'],
        'scopes_supported': ['mcp'],
    })


# ---------------------------------------------------------------------------
# Dynamic client registration — RFC 7591
# ---------------------------------------------------------------------------

@csrf_exempt
@require_http_methods(['POST'])
def oauth_register_client(request):
    """POST /oauth/clients/register/"""
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return _err('invalid_client_metadata', 'Request body must be JSON')

    client_name = body.get('client_name', '').strip()
    redirect_uris = body.get('redirect_uris', [])

    if not client_name:
        return _err('invalid_client_metadata', 'client_name is required')
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _err('invalid_client_metadata', 'redirect_uris must be a non-empty list')

    for uri in redirect_uris:
        if not isinstance(uri, str):
            return _err('invalid_client_metadata', 'Each redirect_uri must be a string')
        if not uri.startswith('https://') and not (settings.DEBUG and uri.startswith('http://localhost')):
            return _err('invalid_client_metadata', f'redirect_uri must use https: {uri}')

    client = OAuthClient.objects.create(client_name=client_name, redirect_uris=redirect_uris)

    return JsonResponse({
        'client_id': client.client_id,
        'client_name': client.client_name,
        'redirect_uris': client.redirect_uris,
        'token_endpoint_auth_method': 'none',
        'grant_types': ['authorization_code'],
        'response_types': ['code'],
    }, status=201)


# ---------------------------------------------------------------------------
# Authorization endpoint
# ---------------------------------------------------------------------------

@login_required
@require_http_methods(['GET', 'POST'])
def oauth_authorize(request):
    """GET/POST /oauth/authorize/ — shows consent screen, issues auth code."""
    params = request.GET if request.method == 'GET' else request.POST

    client_id = params.get('client_id', '').strip()
    redirect_uri = params.get('redirect_uri', '').strip()
    response_type = params.get('response_type', '').strip()
    code_challenge = params.get('code_challenge', '').strip()
    code_challenge_method = params.get('code_challenge_method', '').strip()
    state = params.get('state', '')

    # Validate client
    try:
        client = OAuthClient.objects.get(client_id=client_id)
    except OAuthClient.DoesNotExist:
        return JsonResponse({'error': 'invalid_request', 'error_description': 'Unknown client_id'}, status=400)

    # Validate redirect_uri before using it in any redirect
    if redirect_uri not in client.redirect_uris:
        return JsonResponse({'error': 'invalid_request', 'error_description': 'redirect_uri not registered for this client'}, status=400)

    def _redirect_error(error_code, description):
        qs = urlencode({'error': error_code, 'error_description': description, 'state': state})
        return redirect(f'{redirect_uri}?{qs}')

    if response_type != 'code':
        return _redirect_error('unsupported_response_type', 'Only response_type=code is supported')
    if not code_challenge:
        return _redirect_error('invalid_request', 'code_challenge is required')
    if code_challenge_method != 'S256':
        return _redirect_error('invalid_request', 'Only code_challenge_method=S256 is supported')

    org = get_organization(request)
    if org is None:
        return redirect('tickets:login')

    if request.method == 'GET':
        return render(request, 'tickets/oauth_consent.html', {
            'client': client,
            'org': org,
            'redirect_uri': redirect_uri,
            'code_challenge': code_challenge,
            'state': state,
        })

    # POST — user submitted the consent form
    action = request.POST.get('action')

    if action == 'deny':
        qs = urlencode({'error': 'access_denied', 'error_description': 'User denied access', 'state': state})
        return redirect(f'{redirect_uri}?{qs}')

    if action != 'approve':
        return _redirect_error('invalid_request', 'Invalid form action')

    auth_code = OAuthAuthorizationCode.objects.create(
        client=client,
        organization=org,
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
        expires_at=timezone.now() + timedelta(minutes=10),
    )

    qs = urlencode({'code': auth_code.code, 'state': state})
    return redirect(f'{redirect_uri}?{qs}')


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------

@csrf_exempt
@require_http_methods(['POST'])
def oauth_token(request):
    """POST /oauth/token/ — exchanges auth code for access token (PKCE/S256)."""
    grant_type = request.POST.get('grant_type', '')
    if grant_type != 'authorization_code':
        return _err('unsupported_grant_type', 'Only authorization_code is supported')

    code_value = request.POST.get('code', '').strip()
    redirect_uri = request.POST.get('redirect_uri', '').strip()
    client_id = request.POST.get('client_id', '').strip()
    code_verifier = request.POST.get('code_verifier', '').strip()

    if not all([code_value, redirect_uri, client_id, code_verifier]):
        return _err('invalid_request', 'code, redirect_uri, client_id, and code_verifier are all required')

    try:
        auth_code = (
            OAuthAuthorizationCode.objects
            .select_related('client', 'organization')
            .get(code=code_value, used=False, expires_at__gt=timezone.now())
        )
    except OAuthAuthorizationCode.DoesNotExist:
        return _err('invalid_grant', 'Code not found or expired')

    if auth_code.client.client_id != client_id:
        return _err('invalid_client', 'client_id does not match the code', status=401)

    if auth_code.redirect_uri != redirect_uri:
        return _err('invalid_grant', 'redirect_uri does not match')

    # PKCE S256 verification — RFC 7636 §4.6
    digest = hashlib.sha256(code_verifier.encode('ascii')).digest()
    computed_challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    if computed_challenge != auth_code.code_challenge:
        return _err('invalid_grant', 'code_verifier does not match code_challenge')

    OAuthAuthorizationCode.objects.filter(pk=auth_code.pk).update(used=True)

    access_token = OAuthAccessToken.objects.create(
        client=auth_code.client,
        organization=auth_code.organization,
        expires_at=timezone.now() + timedelta(days=30),
    )

    return JsonResponse({
        'access_token': access_token.token,
        'token_type': 'Bearer',
        'expires_in': 30 * 24 * 3600,
        'scope': 'mcp',
        'resource': f'{_site_url()}/mcp',
    })


# ---------------------------------------------------------------------------
# Token revocation — RFC 7009
# ---------------------------------------------------------------------------

@csrf_exempt
@require_http_methods(['POST'])
def oauth_revoke(request):
    """POST /oauth/revoke/ — revokes an access token. Always returns 200."""
    token_value = request.POST.get('token', '')
    if token_value:
        OAuthAccessToken.objects.filter(token=token_value).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
    return JsonResponse({})
