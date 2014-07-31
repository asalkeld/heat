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

import functools

from oslo.config import cfg
from oslo import messaging

from heat.common import context
from heat.common import exception
from heat.common import messaging as rpc_messaging
from heat.db import api as db_api
from heat.openstack.common.gettextutils import _
from heat.openstack.common import log as logging
from heat.openstack.common import service

LOG = logging.getLogger(__name__)


def request_context(func):
    @functools.wraps(func)
    def wrapped(self, ctx, *args, **kwargs):
        if ctx is not None and not isinstance(ctx, context.RequestContext):
            ctx = context.RequestContext.from_dict(ctx.to_dict())
        try:
            return func(self, ctx, *args, **kwargs)
        except exception.HeatException:
            raise messaging.rpc.dispatcher.ExpectedException()
    return wrapped


class ObserverService(service.Service):

    RPC_API_VERSION = '1.0'

    def __init__(self, host, topic, manager=None):
        super(ObserverService, self).__init__()
        self.host = host
        self.topic = topic
        # The following are initialized here, but assigned in start() which
        # happens after the fork when spawning multiple worker processes
        self.target = None

    def start(self):
        LOG.info(_("Attempting to start observer service..."))
        target = messaging.Target(
            version=self.RPC_API_VERSION, server=cfg.CONF.host,
            topic=self.topic)
        self.target = target
        server = rpc_messaging.get_rpc_server(target, self)
        try:
            server.start()
            super(ObserverService, self).start()
        except Exception:
            LOG.error(_("Failed to start the observer service"))
            return

        LOG.info(_("Started the Observer Service successfully"))

    def stop(self):
        # Stop rpc connection at first for preventing new requests
        LOG.info(_("Attempting to stop observer service..."))
        try:
            self.conn.close()
        except Exception:
            pass

        super(ObserverService, self).stop()
        LOG.info(_("Stopped Observer Service"))

    #TODO(sirushti): Placeholder stuff, need to write calls that are more
    # meaningful here.
    @request_context
    def create_observed_resource(self, context, values):
        return db_api.resource_observed_create(context, values)

    @request_context
    def get_observed_resource(self, context, resource_observed_id):
        return db_api.resource_observed_get(context, resource_observed_id)

    @request_context
    def delete_observed_resource(self, context, resource_observed_id):
        db_api.resource_observed_delete(context, resource_observed_id)

    @request_context
    def create_observed_resource_properties(self, context, values):
        return db_api.resource_properties_observed_create(context, values)

    @request_context
    def get_observed_resource_properties(self, context,
                                         resource_properties_observed_id):
        return db_api.resource_properties_observed_get(
            context, resource_properties_observed_id)

    @request_context
    def update_observed_resource_properties(self, context,
                                            resource_properties_observed_id,
                                            values):
        return db_api.resource_properties_observed_update(
            context, resource_properties_observed_id, values)

    @request_context
    def delete_observed_properties_resource(self, context,
                                            resource_properties_observed_id):
        db_api.resource_properties_observed_delete(
            context, resource_properties_observed_id)
