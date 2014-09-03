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

import copy
import mock
import mox
import six

from oslo.config import cfg

from heat.common import exception
from heat.common import identifier
from heat.common import short_id
from heat.common import template_format
from heat.engine import parser
from heat.engine import resource
from heat.engine import resources
from heat.engine import stack_resource
from heat.engine.resources import instance
from heat.engine import rsrc_defn
from heat.engine import scheduler
from heat.rpc import client as rpc_client
from heat.tests.common import HeatTestCase
from heat.tests import utils


ig_template = '''
{
  "AWSTemplateFormatVersion" : "2010-09-09",
  "Description" : "Template to create multiple instances.",
  "Parameters" : {},
  "Resources" : {
    "JobServerGroup" : {
      "Type" : "OS::Heat::InstanceGroup",
      "Properties" : {
        "LaunchConfigurationName" : { "Ref" : "JobServerConfig" },
        "Size" : "1",
        "AvailabilityZones" : ["nova"]
      }
    },

    "JobServerConfig" : {
      "Type" : "AWS::AutoScaling::LaunchConfiguration",
      "Metadata": {"foo": "bar"},
      "Properties": {
        "ImageId"           : "foo",
        "InstanceType"      : "m1.large",
        "KeyName"           : "test",
        "SecurityGroups"    : [ "sg-1" ],
        "UserData"          : "jsconfig data"
      }
    }
  }
}
'''


# FIXME(shardy) this is duplicated with test_scheduler, but will be
# removed when nested stack updates get converted to RPC
class DummyTask(object):
    def __init__(self, num_steps=3):
        self.num_steps = num_steps

    def __call__(self, *args, **kwargs):
        for i in range(1, self.num_steps + 1):
            self.do_step(i, *args, **kwargs)
            yield

    def do_step(self, step_num, *args, **kwargs):
        pass


class InstanceGroupTest(HeatTestCase):
    def setUp(self):
        super(InstanceGroupTest, self).setUp()
        self.context = utils.dummy_context()
        self.stack = None
        self.nested_stack = None

    def _stub_create_template(self, num, num_existing=0, stub=True):
        print "SHDEBUG in _stub_create_template num=%s, num_existing=%s" % (num, num_existing)
        template = {'HeatTemplateFormatVersion': '2012-12-12',
                    'Resources': {}}
        resource_template = {
            'Metadata': {u'foo': u'bar'},
            'Type': 'OS::Heat::ScaledResource',
            'Properties': {'UserData': 'jsconfig data',
                           'Tags': [{'Key': 'metering.groupname',
                                     'Value': self.n_stack_name}],
                           'ImageId': u'foo',
                           'KeyName': u'test',
                           'SecurityGroups': [u'sg-1'],
                           'InstanceType': u'm1.large'},
            'DependsOn': []}

        if stub:
            self.m.StubOutWithMock(short_id, 'generate_id')
            self.m.StubOutWithMock(short_id, 'get_id')
            physid_suffix = 'xyz0'
            short_id.get_id(mox.IgnoreArg()).MultipleTimes().AndReturn(
                physid_suffix)

        cookie = object()
        for ii in range(num):
            rsrc_name = 'aabbcc%s' % ii
            print "SHDEBUG stubbing short_id=%s" % rsrc_name
            if ii >= num_existing:
                short_id.generate_id().AndReturn(rsrc_name)
            template['Resources'][rsrc_name] = resource_template
        return template

    def _stub_create(self, num):
        self.stub_KeypairConstraint_validate()
        self.stub_ImageConstraint_validate()

        self.n_stack_name = 'test_stack-JobServerGroup-xyz0'
        template = self._stub_create_template(num)

        params = {'parameters': {},
                 'resource_registry': {
                    'OS::Heat::ScaledResource': 'AWS::EC2::Instance',
                    'resources': {}}}

        # Because the nested stack creation via RPC is stubbed, we
        # create the nested stack record in the DB directly, or the
        # subsequent instrospection via nested() won't work
        self.nested_stack = utils.parse_stack(
            template, params, self.n_stack_name,
            ctx=self.context, owner_id=self.stack.id,
            user_creds_id=self.stack.user_creds_id,
            nested_depth=1)
        identity = identifier.HeatIdentifier(self.context.tenant,
                                             self.n_stack_name,
                                             self.nested_stack.id)

        args = {'disable_rollback': True, 'adopt_stack_data': None,
                'timeout_mins': None}

        self.m.StubOutWithMock(rpc_client.EngineClient, 'call')
        rpc_client.EngineClient.call(
            self.context,
            ('create_stack',
             {'stack_name': self.n_stack_name,
              'template': template,
              'params': params,
              'files': {},
              'args': args,
              'owner_id': self.stack.id,
              'nested_depth': 1,
              'stack_user_project_id': self.stack.stack_user_project_id,
              'user_creds_id': self.stack.user_creds_id})
        ).AndReturn(dict(identity))
        self.m.ReplayAll()

    def create_resource(self, t, stack, resource_name,
                        status=parser.Stack.COMPLETE):
        # subsequent resources may need to reference previous created resources
        # use the stack's resource objects instead of instantiating new ones
        if self.nested_stack:
            self.nested_stack.state_set(parser.Stack.CREATE,
                                        status, 'create %s' % status)

        rsrc = stack[resource_name]
        self.assertIsNone(rsrc.validate())
        scheduler.TaskRunner(rsrc.create)()
        self.assertEqual((rsrc.CREATE, status), rsrc.state)
        return rsrc

    def test_instance_group(self):

        t = template_format.parse(ig_template)
        self.stack = utils.parse_stack(t, ctx=self.context)

        # start with min then delete
        self._stub_create(1)
        self.m.StubOutWithMock(instance.Instance, 'FnGetAtt')
        instance.Instance.FnGetAtt('PublicIp').AndReturn('1.2.3.4')

        self.m.ReplayAll()
        self.create_resource(t, self.stack, 'JobServerConfig')
        rsrc = self.create_resource(t, self.stack, 'JobServerGroup')
        self.assertEqual(self.n_stack_name, rsrc.FnGetRefId())
        self.assertEqual('1.2.3.4', rsrc.FnGetAtt('InstanceList'))

        nested = rsrc.nested()
        self.assertEqual(nested.id, rsrc.resource_id)

        rsrc.delete()
        self.m.VerifyAll()

    @mock.patch(
        'heat.engine.stack_resource.StackResource.update_with_template')
    @mock.patch(
        'heat.engine.stack_resource.StackResource.check_update_complete')
    def test_handle_update_size(self, mock_update_complete, mock_update):
        t = template_format.parse(ig_template)
        properties = t['Resources']['JobServerGroup']['Properties']
        properties['Size'] = '2'
        self.stack = utils.parse_stack(t, ctx=self.context)

        self._stub_create(2)

        self.m.StubOutWithMock(instance.Instance, 'FnGetAtt')
        instance.Instance.FnGetAtt('PublicIp').AndReturn('10.0.0.2')
        instance.Instance.FnGetAtt('PublicIp').AndReturn('10.0.0.3')

        self.m.ReplayAll()

        self.create_resource(t, self.stack, 'JobServerConfig')
        rsrc = self.create_resource(t, self.stack, 'JobServerGroup')
        self.assertEqual('10.0.0.2,10.0.0.3', rsrc.FnGetAtt('InstanceList'))

        self.m.VerifyAll()
        self.m.UnsetStubs()
        dtr = scheduler.TaskRunner(DummyTask())
        dtr.start()
        mock_update.return_value = dtr
        mock_update_complete.return_value = True
        update_template = self._stub_create_template(5, 2)
        update_environment = {
            'parameters': {},
                 'resource_registry': {
                      'OS::Heat::ScaledResource': 'AWS::EC2::Instance'}}
        self.m.ReplayAll()

        props = copy.copy(rsrc.properties.data)
        props['Size'] = 5
        update_snippet = rsrc_defn.ResourceDefinition(rsrc.name,
                                                      rsrc.type(),
                                                      props)
        scheduler.TaskRunner(rsrc.update, update_snippet)()
        self.assertEqual(update_template, mock_update.call_args[0][0].t)
        self.assertEqual(update_environment, mock_update.call_args[0][1])
        self.assertEqual((rsrc.UPDATE, rsrc.COMPLETE), rsrc.state)

        rsrc.delete()
        self.m.VerifyAll()

    def test_create_error_failed(self):
        cfg.CONF.set_override('action_retry_limit', 0)
        t = template_format.parse(ig_template)
        self.stack = utils.parse_stack(t, ctx=self.context)

        self._stub_create(1)
        self.m.ReplayAll()
        self.create_resource(t, self.stack, 'JobServerConfig')
        exc = self.assertRaises(exception.ResourceFailure,
                                self.create_resource,
                                t, self.stack, 'JobServerGroup',
                                status=resource.Resource.FAILED)

        self.assertIn('reason: create FAILED', six.text_type(exc))
        self.m.VerifyAll()

    def test_create_error_wrongaction(self):
        cfg.CONF.set_override('action_retry_limit', 0)
        t = template_format.parse(ig_template)
        self.stack = utils.parse_stack(t, ctx=self.context)

        self._stub_create(1)
        self.m.ReplayAll()
        self.create_resource(t, self.stack, 'JobServerConfig')
        self.nested_stack.state_set(parser.Stack.SUSPEND,
                                    parser.Stack.COMPLETE,
                                    'suspend complete')

        rsrc = self.stack['JobServerGroup']
        self.assertIsNone(rsrc.validate())
        exc = self.assertRaises(exception.ResourceFailure,
                                scheduler.TaskRunner(rsrc.create))
        self.assertIn('Unknown status SUSPEND', six.text_type(exc))
        self.m.VerifyAll()

    @mock.patch(
        'heat.engine.stack_resource.StackResource.update_with_template')
    @mock.patch(
        'heat.engine.stack_resource.StackResource.check_update_complete')
    def test_update_error(self, mock_update_complete, mock_update):
        t = template_format.parse(ig_template)
        properties = t['Resources']['JobServerGroup']['Properties']
        properties['Size'] = '2'
        self.stack = utils.parse_stack(t, ctx=self.context)

        self._stub_create(2)

        self.m.StubOutWithMock(instance.Instance, 'FnGetAtt')
        instance.Instance.FnGetAtt('PublicIp').AndReturn('10.0.0.2')
        instance.Instance.FnGetAtt('PublicIp').AndReturn('10.0.0.3')

        self.m.ReplayAll()

        self.create_resource(t, self.stack, 'JobServerConfig')
        rsrc = self.create_resource(t, self.stack, 'JobServerGroup')
        self.assertEqual('10.0.0.2,10.0.0.3', rsrc.FnGetAtt('InstanceList'))

        self.m.VerifyAll()
        self.m.UnsetStubs()
        dtr = scheduler.TaskRunner(DummyTask())
        dtr.start()
        mock_update.return_value = dtr
        mock_update_complete.side_effect = Exception('Update fail')
        update_template = self._stub_create_template(5, 2)
        update_environment = {
            'parameters': {},
                 'resource_registry': {
                      'OS::Heat::ScaledResource': 'AWS::EC2::Instance'}}
        self.m.ReplayAll()

        props = copy.copy(rsrc.properties.data)
        props['Size'] = 5
        update_snippet = rsrc_defn.ResourceDefinition(rsrc.name,
                                                      rsrc.type(),
                                                      props)
        updater = scheduler.TaskRunner(rsrc.update, update_snippet)
        exc = self.assertRaises(exception.ResourceFailure, updater)
        self.assertIn('Update fail', six.text_type(exc))
        self.assertEqual((rsrc.UPDATE, rsrc.FAILED), rsrc.state)

        rsrc.delete()
        self.m.VerifyAll()

    def test_update_fail_badprop(self):
        t = template_format.parse(ig_template)
        properties = t['Resources']['JobServerGroup']['Properties']
        properties['Size'] = '2'
        self.stack = utils.parse_stack(t, ctx=self.context)

        self._stub_create(2)

        self.m.ReplayAll()

        self.create_resource(t, self.stack, 'JobServerConfig')
        rsrc = self.create_resource(t, self.stack, 'JobServerGroup')

        props = copy.copy(rsrc.properties.data)
        props['AvailabilityZones'] = ['wibble']
        update_snippet = rsrc_defn.ResourceDefinition(rsrc.name,
                                                      rsrc.type(),
                                                      props)
        updater = scheduler.TaskRunner(rsrc.update, update_snippet)
        self.assertRaises(resource.UpdateReplace, updater)
        rsrc.delete()
        self.m.VerifyAll()

    def test_update_config_metadata(self):
        t = template_format.parse(ig_template)
        properties = t['Resources']['JobServerGroup']['Properties']
        properties['Size'] = '2'
        self.stack = utils.parse_stack(t, ctx=self.context)

        self._stub_create(2)
        self.m.ReplayAll()
        rsrc = self.create_resource(t, self.stack, 'JobServerConfig')
        self.create_resource(t, self.stack, 'JobServerGroup')

        props = copy.copy(rsrc.properties.data)
        metadata = copy.copy(rsrc.metadata_get())

        update_snippet = rsrc_defn.ResourceDefinition(rsrc.name,
                                                      rsrc.type(),
                                                      props,
                                                      metadata)
        # Change nothing in the first update
        scheduler.TaskRunner(rsrc.update, update_snippet)()

        self.assertEqual('bar', metadata['foo'])
        metadata['foo'] = 'wibble'
        update_snippet = rsrc_defn.ResourceDefinition(rsrc.name,
                                                      rsrc.type(),
                                                      props,
                                                      metadata)
        # Changing metadata in the second update triggers UpdateReplace
        updater = scheduler.TaskRunner(rsrc.update, update_snippet)
        self.assertRaises(resource.UpdateReplace, updater)

        self.m.VerifyAll()class TestChildTemplate(common.HeatTestCase):
    params = {'KeyName': 'test', 'ImageId': 'foo'}

    def setUp(self):
        super(TestChildTemplate, self).setUp()

        t = template_format.parse(inline_templates.as_template)
        stack = utils.parse_stack(t, params=self.params)

        defn = rsrc_defn.ResourceDefinition('ig', 'OS::Heat::InstanceGroup',
                                            {'Size': 2,
                                             'LaunchConfigurationName': 'foo'})
        self.instance_group = asc.InstanceGroup('ig', defn, stack)

    def test_child_template(self):
        self.instance_group._create_template = mock.Mock(return_value='tpl')

        self.assertEqual('tpl', self.instance_group.child_template())
        self.instance_group._create_template.assert_called_once_with(2)

    def test_child_params(self):
        self.instance_group._environment = mock.Mock(return_value='env')
        self.assertEqual('env', self.instance_group.child_params())
