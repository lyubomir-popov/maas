# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""API handler: `Account`."""

__all__ = [
    'AccountHandler',
    ]

import http.client
import json

from django.http import HttpResponse
from maasserver.api.support import (
    operation,
    OperationsHandler,
)
from maasserver.api.utils import get_mandatory_param
from piston3.utils import rc


class AccountHandler(OperationsHandler):
    """Manage the current logged-in user."""
    api_doc_section_name = "Logged-in user"
    create = read = update = delete = None

    @operation(idempotent=False)
    def create_authorisation_token(self, request):
        """Create an authorisation OAuth token and OAuth consumer.

        :return: a json dict with three keys: 'token_key',
            'token_secret' and 'consumer_key' (e.g.
            {token_key: 's65244576fgqs', token_secret: 'qsdfdhv34',
            consumer_key: '68543fhj854fg'}).
        :rtype: string (json)

        """
        profile = request.user.userprofile
        consumer, token = profile.create_authorisation_token()
        auth_info = {
            'token_key': token.key, 'token_secret': token.secret,
            'consumer_key': consumer.key,
            }
        return HttpResponse(
            json.dumps(auth_info),
            content_type='application/json; charset=utf-8',
            status=int(http.client.OK))

    @operation(idempotent=False)
    def delete_authorisation_token(self, request):
        """Delete an authorisation OAuth token and the related OAuth consumer.

        :param token_key: The key of the token to be deleted.
        :type token_key: unicode
        """
        profile = request.user.userprofile
        token_key = get_mandatory_param(request.data, 'token_key')
        profile.delete_authorisation_token(token_key)
        return rc.DELETED

    @classmethod
    def resource_uri(cls, *args, **kwargs):
        return ('account_handler', [])