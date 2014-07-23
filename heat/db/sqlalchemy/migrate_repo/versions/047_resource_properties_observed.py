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

import sqlalchemy


def upgrade(migrate_engine):
    meta = sqlalchemy.MetaData()
    meta.bind = migrate_engine

    resource_observed = sqlalchemy.Table(
        'resource_observed', meta,
        sqlalchemy.Column('created_at', sqlalchemy.DateTime),
        sqlalchemy.Column('updated_at', sqlalchemy.DateTime),
        sqlalchemy.Column('id',
                          sqlalchemy.String(36),
                          primary_key=True,
                          nullable=False),
        sqlalchemy.Column('name', sqlalchemy.String(255), nullable=True),
        sqlalchemy.Column('nova_instance', sqlalchemy.String(255)),
        sqlalchemy.Column('stack_id',
                          sqlalchemy.String(36),
                          sqlalchemy.ForeignKey('stack.id'),
                          nullable=False),
        mysql_engine='InnoDB',
        mysql_charset='utf8'
    )
    sqlalchemy.Table('stack', meta, autoload=True)
    resource_observed.create()

    resource_properties_observed = sqlalchemy.Table(
        'resource_properties_observed', meta,
        sqlalchemy.Column('created_at', sqlalchemy.DateTime),
        sqlalchemy.Column('updated_at', sqlalchemy.DateTime),
        sqlalchemy.Column('id',
                          sqlalchemy.String(36),
                          primary_key=True,
                          nullable=False),
        sqlalchemy.Column('stack_id',
                          sqlalchemy.String(36),
                          sqlalchemy.ForeignKey('stack.id'),
                          nullable=False),
        sqlalchemy.Column('resource_observed_id',
                          sqlalchemy.String(36),
                          sqlalchemy.ForeignKey('resource_observed.id'),
                          nullable=False),
        sqlalchemy.Column('resource_name', sqlalchemy.String(255),
                          nullable=True),
        sqlalchemy.Column('prop_name', sqlalchemy.Text),
        sqlalchemy.Column('prop_value', sqlalchemy.Text),
        mysql_engine='InnoDB',
        mysql_charset='utf8'
    )
    resource_properties_observed.create()


def downgrade(migrate_engine):
    meta = sqlalchemy.MetaData()
    meta.bind = migrate_engine

    resource_properties_observed = sqlalchemy.Table(
        'resource_properties_observed', meta, autoload=True)
    resource_properties_observed.drop()

    resource_observed = sqlalchemy.Table(
        'resource_observed', meta, autoload=True)
    resource_observed.drop()
