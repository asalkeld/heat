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
import six
import warnings
import webob

from heat.common import context
from heat.common import exception
from heat.common import identifier
from heat.common import messaging as rpc_messaging
from heat.db import api as db_api
from heat.engine import api
from heat.engine import environment
from heat.engine.event import Event
from heat.engine import parameter_groups
from heat.engine import parser
from heat.engine import properties
from heat.engine import resources
from heat.engine import template as templatem
from heat.openstack.common.gettextutils import _
from heat.openstack.common import log as logging
from heat.openstack.common import service
from heat.openstack.common import uuidutils
from heat.rpc import api as rpc_api

cfg.CONF.import_opt('max_resources_per_stack', 'heat.common.config')
cfg.CONF.import_opt('max_stacks_per_tenant', 'heat.common.config')

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


class EngineService(service.Service):
    """
    Manages the running instances from creation to destruction.
    All the methods in here are called from the RPC backend.  This is
    all done dynamically so if a call is made via RPC that does not
    have a corresponding method here, an exception will be thrown when
    it attempts to call into this class.  Arguments to these methods
    are also dynamically added and will be named as keyword arguments
    by the RPC caller.
    """

    RPC_API_VERSION = '1.1'

    def __init__(self, host, topic, manager=None):
        super(EngineService, self).__init__()
        resources.initialise()
        self.host = host
        self.topic = topic

        # The following are initialized here, but assigned in start() which
        # happens after the fork when spawning multiple worker processes
        self.target = None

        if cfg.CONF.instance_user:
            warnings.warn('The "instance_user" option in heat.conf is '
                          'deprecated and will be removed in the Juno '
                          'release.', DeprecationWarning)

    def start(self):
        target = messaging.Target(
            version=self.RPC_API_VERSION, server=cfg.CONF.host,
            topic=self.topic)
        self.target = target
        server = rpc_messaging.get_rpc_server(target, self)
        server.start()
        self._client = rpc_messaging.get_rpc_client(
            version=self.RPC_API_VERSION)

        super(EngineService, self).start()

    def stop(self):
        # Stop rpc connection at first for preventing new requests
        LOG.info(_("Attempting to stop engine service..."))
        try:
            self.conn.close()
        except Exception:
            pass

        # Terminate the engine process
        LOG.info(_("All threads were gone, terminating engine"))
        super(EngineService, self).stop()

    @request_context
    def identify_stack(self, cnxt, stack_name):
        """
        The identify_stack method returns the full stack identifier for a
        single, live stack given the stack name.

        :param cnxt: RPC context.
        :param stack_name: Name or UUID of the stack to look up.
        """
        if uuidutils.is_uuid_like(stack_name):
            s = db_api.stack_get(cnxt, stack_name, show_deleted=True)
            # may be the name is in uuid format, so if get by id returns None,
            # we should get the info by name again
            if not s:
                s = db_api.stack_get_by_name(cnxt, stack_name)
        else:
            s = db_api.stack_get_by_name(cnxt, stack_name)
        if s:
            stack = parser.Stack.load(cnxt, stack=s)
            return dict(stack.identifier())
        else:
            raise exception.StackNotFound(stack_name=stack_name)

    def _get_stack(self, cnxt, stack_identity, show_deleted=False):
        identity = identifier.HeatIdentifier(**stack_identity)

        s = db_api.stack_get(cnxt, identity.stack_id,
                             show_deleted=show_deleted,
                             eager_load=True)

        if s is None:
            raise exception.StackNotFound(stack_name=identity.stack_name)

        if cnxt.tenant_id not in (identity.tenant, s.stack_user_project_id):
            # The DB API should not allow this, but sanity-check anyway..
            raise exception.InvalidTenant(target=identity.tenant,
                                          actual=cnxt.tenant_id)

        if identity.path or s.name != identity.stack_name:
            raise exception.StackNotFound(stack_name=identity.stack_name)

        return s

    @request_context
    def show_stack(self, cnxt, stack_identity):
        """
        Return detailed information about one or all stacks.

        :param cnxt: RPC context.
        :param stack_identity: Name of the stack you want to show, or None
            to show all
        """
        if stack_identity is not None:
            db_stack = self._get_stack(cnxt, stack_identity, show_deleted=True)
            stacks = [parser.Stack.load(cnxt, stack=db_stack)]
        else:
            stacks = parser.Stack.load_all(cnxt)

        return [api.format_stack(stack) for stack in stacks]

    def get_revision(self, cnxt):
        return cfg.CONF.revision['heat_revision']

    @request_context
    def list_stacks(self, cnxt, limit=None, marker=None, sort_keys=None,
                    sort_dir=None, filters=None, tenant_safe=True,
                    show_deleted=False):
        """
        The list_stacks method returns attributes of all stacks.  It supports
        pagination (``limit`` and ``marker``), sorting (``sort_keys`` and
        ``sort_dir``) and filtering (``filters``) of the results.

        :param cnxt: RPC context
        :param limit: the number of stacks to list (integer or string)
        :param marker: the ID of the last item in the previous page
        :param sort_keys: an array of fields used to sort the list
        :param sort_dir: the direction of the sort ('asc' or 'desc')
        :param filters: a dict with attribute:value to filter the list
        :param tenant_safe: if true, scope the request by the current tenant
        :param show_deleted: if true, show soft-deleted stacks
        :returns: a list of formatted stacks
        """
        stacks = parser.Stack.load_all(cnxt, limit, marker, sort_keys,
                                       sort_dir, filters, tenant_safe,
                                       show_deleted, resolve_data=False)
        return [api.format_stack(stack) for stack in stacks]

    @request_context
    def count_stacks(self, cnxt, filters=None, tenant_safe=True,
                     show_deleted=False):
        """
        Return the number of stacks that match the given filters
        :param cnxt: RPC context.
        :param filters: a dict of ATTR:VALUE to match against stacks
        :param tenant_safe: if true, scope the request by the current tenant
        :param show_deleted: if true, count will include the deleted stacks
        :returns: a integer representing the number of matched stacks
        """
        return db_api.stack_count_all(cnxt, filters=filters,
                                      tenant_safe=tenant_safe,
                                      show_deleted=show_deleted)

    def _validate_new_stack(self, cnxt, stack_name, parsed_template):
        if db_api.stack_get_by_name(cnxt, stack_name):
            raise exception.StackExists(stack_name=stack_name)

        tenant_limit = cfg.CONF.max_stacks_per_tenant
        if db_api.stack_count_all(cnxt) >= tenant_limit:
            message = _("You have reached the maximum stacks per tenant, %d."
                        " Please delete some stacks.") % tenant_limit
            raise exception.RequestLimitExceeded(message=message)

        num_resources = len(parsed_template[parsed_template.RESOURCES])
        if num_resources > cfg.CONF.max_resources_per_stack:
            message = exception.StackResourceLimitExceeded.msg_fmt
            raise exception.RequestLimitExceeded(message=message)

    def _parse_template_and_validate_stack(self, cnxt, stack_name, template,
                                           params, files, args, owner_id=None):
        tmpl = templatem.Template(template, files=files)
        self._validate_new_stack(cnxt, stack_name, tmpl)

        common_params = api.extract_args(args)
        env = environment.Environment(params)
        stack = parser.Stack(cnxt, stack_name, tmpl, env,
                             owner_id=owner_id,
                             **common_params)

        self._validate_deferred_auth_context(cnxt, stack)
        stack.validate()
        return stack

    @request_context
    def create_stack(self, cnxt, stack_name, template, params, files, args,
                     owner_id=None):
        """
        The create_stack method creates a new stack using the template
        provided.
        Note that at this stage the template has already been fetched from the
        heat-api process if using a template-url.

        :param cnxt: RPC context.
        :param stack_name: Name of the stack you want to create.
        :param template: Template of stack you want to create.
        :param params: Stack Input Params
        :param files: Files referenced from the template
        :param args: Request parameters/args passed from API
        :param owner_id: parent stack ID for nested stacks, only expected when
                         called from another heat-engine (not a user option)
        """
        LOG.info(_('Creating stack %s') % stack_name)

        stack = self._parse_template_and_validate_stack(cnxt,
                                                        stack_name,
                                                        template,
                                                        params,
                                                        files,
                                                        args,
                                                        owner_id)

        stack.store()
        stack.create()

        return dict(stack.identifier())

    @request_context
    def update_stack(self, cnxt, stack_identity, template, params,
                     files, args):
        """
        The update_stack method updates an existing stack based on the
        provided template and parameters.
        Note that at this stage the template has already been fetched from the
        heat-api process if using a template-url.

        :param cnxt: RPC context.
        :param stack_identity: Name of the stack you want to create.
        :param template: Template of stack you want to create.
        :param params: Stack Input Params
        :param files: Files referenced from the template
        :param args: Request parameters/args passed from API
        """
        # Get the database representation of the existing stack
        db_stack = self._get_stack(cnxt, stack_identity)
        LOG.info(_('Updating stack %s') % db_stack.name)

        current_stack = parser.Stack.load(cnxt, stack=db_stack)

        if current_stack.action == current_stack.SUSPEND:
            msg = _('Updating a stack when it is suspended')
            raise exception.NotSupported(feature=msg)

        # Now parse the template and any parameters for the updated
        # stack definition.
        tmpl = templatem.Template(template, files=files)
        if len(tmpl[tmpl.RESOURCES]) > cfg.CONF.max_resources_per_stack:
            raise exception.RequestLimitExceeded(
                message=exception.StackResourceLimitExceeded.msg_fmt)
        stack_name = current_stack.name
        common_params = api.extract_args(args)
        common_params.setdefault(rpc_api.PARAM_TIMEOUT,
                                 current_stack.timeout_mins)
        env = environment.Environment(params)
        updated_stack = parser.Stack(cnxt, stack_name, tmpl,
                                     env, **common_params)
        updated_stack.parameters.set_stack_id(current_stack.identifier())

        self._validate_deferred_auth_context(cnxt, updated_stack)
        updated_stack.validate()
        current_stack.update(updated_stack)

        return dict(current_stack.identifier())

    @request_context
    def validate_template(self, cnxt, template, params=None):
        """
        The validate_template method uses the stack parser to check
        the validity of a template.

        :param cnxt: RPC context.
        :param template: Template of stack you want to create.
        :param params: Stack Input Params
        """
        LOG.info(_('validate_template'))
        if template is None:
            msg = _("No Template provided.")
            return webob.exc.HTTPBadRequest(explanation=msg)

        tmpl = templatem.Template(template)

        # validate overall template
        try:
            tmpl.validate()
        except Exception as ex:
            return {'Error': six.text_type(ex)}

        # validate resource classes
        tmpl_resources = tmpl[tmpl.RESOURCES]

        env = environment.Environment(params)

        for res in tmpl_resources.values():
            ResourceClass = env.get_class(res['Type'])
            if ResourceClass == resources.template_resource.TemplateResource:
                # we can't validate a TemplateResource unless we instantiate
                # it as we need to download the template and convert the
                # parameters into properties_schema.
                continue

            props = properties.Properties(ResourceClass.properties_schema,
                                          res.get('Properties', {}),
                                          context=cnxt)
            deletion_policy = res.get('DeletionPolicy', 'Delete')
            try:
                ResourceClass.validate_deletion_policy(deletion_policy)
                props.validate(with_value=False)
            except Exception as ex:
                return {'Error': six.text_type(ex)}

        # validate parameters
        tmpl_params = tmpl.parameters(None, {})
        tmpl_params.validate(validate_value=False, context=cnxt)
        is_real_param = lambda p: p.name not in tmpl_params.PSEUDO_PARAMETERS
        params = tmpl_params.map(api.format_validate_parameter, is_real_param)
        param_groups = parameter_groups.ParameterGroups(tmpl)

        result = {
            'Description': tmpl.get('Description', ''),
            'Parameters': params,
        }

        if param_groups.parameter_groups:
            result['ParameterGroups'] = param_groups.parameter_groups

        return result

    @request_context
    def get_template(self, cnxt, stack_identity):
        """
        Get the template.

        :param cnxt: RPC context.
        :param stack_identity: Name of the stack you want to see.
        """
        s = self._get_stack(cnxt, stack_identity, show_deleted=True)
        if s:
            return s.raw_template.template
        return None

    @request_context
    def delete_stack(self, cnxt, stack_identity):
        st = self._get_stack(cnxt, stack_identity)
        LOG.info(_('Deleting stack %s') % st.name)
        parser.Stack.load(cnxt, stack=st).delete()
        return None

    @request_context
    def list_events(self, cnxt, stack_identity, filters=None, limit=None,
                    marker=None, sort_keys=None, sort_dir=None):
        """
        The list_events method lists all events associated with a given stack.
        It supports pagination (``limit`` and ``marker``),
        sorting (``sort_keys`` and ``sort_dir``) and filtering(filters)
        of the results.

        :param cnxt: RPC context.
        :param stack_identity: Name of the stack you want to get events for
        :param filters: a dict with attribute:value to filter the list
        :param limit: the number of events to list (integer or string)
        :param marker: the ID of the last event in the previous page
        :param sort_keys: an array of fields used to sort the list
        :param sort_dir: the direction of the sort ('asc' or 'desc').
        """

        if stack_identity is not None:
            st = self._get_stack(cnxt, stack_identity, show_deleted=True)

            events = db_api.event_get_all_by_stack(cnxt, st.id, limit=limit,
                                                   marker=marker,
                                                   sort_keys=sort_keys,
                                                   sort_dir=sort_dir,
                                                   filters=filters)
        else:
            events = db_api.event_get_all_by_tenant(cnxt, limit=limit,
                                                    marker=marker,
                                                    sort_keys=sort_keys,
                                                    sort_dir=sort_dir,
                                                    filters=filters)

        stacks = {}

        def get_stack(stack_id):
            if stack_id not in stacks:
                stacks[stack_id] = parser.Stack.load(cnxt, stack_id)
            return stacks[stack_id]

        return [api.format_event(Event.load(cnxt,
                                            e.id, e,
                                            get_stack(e.stack_id)))
                for e in events]

    @request_context
    def describe_stack_resource(self, cnxt, stack_identity, resource_name):
        s = self._get_stack(cnxt, stack_identity)
        stack = parser.Stack.load(cnxt, stack=s)

        if cfg.CONF.heat_stack_user_role in cnxt.roles:
            if not self._authorize_stack_user(cnxt, stack, resource_name):
                LOG.warning(_("Access denied to resource %s") % resource_name)
                raise exception.Forbidden()

        if resource_name not in stack:
            raise exception.ResourceNotFound(resource_name=resource_name,
                                             stack_name=stack.name)

        if stack[resource_name].id is None:
            raise exception.ResourceNotAvailable(resource_name=resource_name)

        return api.format_stack_resource(stack[resource_name])

    @request_context
    def find_physical_resource(self, cnxt, physical_resource_id):
        """
        Return an identifier for the resource with the specified physical
        resource ID.

        :param cnxt: RPC context.
        :param physical_resource_id: The physical resource ID to look up.
        """
        rs = db_api.resource_get_by_physical_resource_id(cnxt,
                                                         physical_resource_id)
        if not rs:
            raise exception.PhysicalResourceNotFound(
                resource_id=physical_resource_id)

        stack = parser.Stack.load(cnxt, stack_id=rs.stack.id)
        return dict(stack[rs.name].identifier())

    @request_context
    def describe_stack_resources(self, cnxt, stack_identity, resource_name):
        s = self._get_stack(cnxt, stack_identity)

        stack = parser.Stack.load(cnxt, stack=s)

        return [api.format_stack_resource(resource)
                for name, resource in six.iteritems(stack)
                if resource_name is None or name == resource_name]

    @request_context
    def list_stack_resources(self, cnxt, stack_identity, nested_depth=0):
        s = self._get_stack(cnxt, stack_identity, show_deleted=True)
        stack = parser.Stack.load(cnxt, stack=s)
        depth = min(nested_depth, cfg.CONF.max_nested_stack_depth)

        return [api.format_stack_resource(resource, detail=False)
                for resource in stack.iter_resources(depth)]

    @request_context
    def stack_check(self, cnxt, stack_identity):
        '''
        Handle request to perform a check action on a stack
        '''
        s = self._get_stack(cnxt, stack_identity)
        stack = parser.Stack.load(cnxt, stack=s)
        LOG.info(_("Checking stack %s") % stack.name)

        self.thread_group_mgr.start_with_lock(cnxt, stack, self.engine_id,
                                              stack.check)
