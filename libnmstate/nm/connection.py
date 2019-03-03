#
# Copyright 2018-2019 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

import logging
import uuid

from . import nmclient
from .active_connection import ActiveConnection


class ConnectionProfile(object):

    def __init__(self, profile=None):
        self._con_profile = profile
        self._nmclient = nmclient.client()
        self._mainloop = nmclient.mainloop()
        self._nmdevice = None
        self._con_id = None

    def create(self, settings):
        self._con_profile = nmclient.NM.SimpleConnection.new()
        for setting in settings:
            self._con_profile.add_setting(setting)

    def import_by_device(self, nmdev):
        self._con_profile = None
        ac = get_device_active_connection(nmdev)
        if ac:
            self._con_profile = ac.props.connection
            self._nmdevice = nmdev

    def import_by_id(self, con_id):
        self._con_profile = None
        if con_id:
            self._con_profile = self._nmclient.get_connection_by_id(con_id)
            self._con_id = con_id

    def update(self, con_profile):
        self._con_profile.replace_settings_from_connection(con_profile.profile)

    def add(self, save_to_disk=True):
        user_data = self._mainloop
        self._mainloop.push_action(
            self._nmclient.add_connection_async,
            self._con_profile,
            save_to_disk,
            self._mainloop.cancellable,
            self._add_connection_callback,
            user_data,
        )

    def commit(self, save_to_disk=True, nmdev=None):
        user_data = self._mainloop, nmdev
        self._mainloop.push_action(
            self._con_profile.commit_changes_async,
            save_to_disk,
            self._mainloop.cancellable,
            self._commit_changes_callback,
            user_data,
        )

    def activate(self, dev=None, connection_id=None):
        self._mainloop.push_action(
            self._safe_activate_async, dev, connection_id)

    @property
    def profile(self):
        return self._con_profile

    @property
    def devname(self):
        if self._con_profile:
            return self._con_profile.get_interface_name()
        return None

    def _safe_activate_async(self, dev, connection_id):
        if connection_id:
            self.import_by_id(connection_id)
        elif dev:
            self.import_by_device(dev)
        elif not self._con_profile:
            err_msg = (
                'Missing base properties: profile={}, id={}, dev={}'.format(
                    self._con_profile, connection_id, dev)
            )
            self._mainloop.quit(err_msg)

        cancellable = self._mainloop.new_cancellable()

        active_conn = get_device_active_connection(dev)
        if active_conn:
            ac = ActiveConnection(active_conn)
            if ac.is_activating:
                logging.debug(
                    'Connection activation in progress: dev=%s, state=%s',
                    ac.devname, ac.state)
                self._waitfor_active_connection_async(ac)
                return

        specific_object = None
        user_data = cancellable
        self._nmclient.activate_connection_async(
            self.profile,
            dev,
            specific_object,
            cancellable,
            self._active_connection_callback,
            user_data,
        )

    def _active_connection_callback(self, src_object, result, user_data):
        cancellable = user_data
        self._mainloop.drop_cancellable(cancellable)

        try:
            nm_act_con = src_object.activate_connection_finish(result)
        except Exception as e:
            act_type, act_object = self._get_activation_metadata()

            if self._mainloop.is_action_canceled(e):
                logging.debug(
                    'Connection activation canceled on %s %s: error=%s',
                    act_type, act_object, e)
            elif self._is_connection_unavailable(e):
                logging.warning('Connection unavailable on %s %s, retrying',
                                act_type, act_object)
                self._mainloop.execute_last_action()
            else:
                self._mainloop.quit(
                    'Connection activation failed on {} {}: error={}'.format(
                        act_type, act_object, e))
            return

        if nm_act_con is None:
            act_type, act_object = self._get_activation_metadata()
            self._mainloop.quit(
                'Connection activation failed on {} {}: error=unknown'.format(
                    act_type, act_object)
            )
        else:
            devname = nm_act_con.props.connection.get_interface_name()
            logging.debug(
                'Connection activation initiated: dev=%s, con-state=%s',
                devname, nm_act_con.props.state)

            ac = ActiveConnection(nm_act_con)
            if ac.is_active:
                self._mainloop.execute_next_action()
            elif ac.is_activating:
                self._waitfor_active_connection_async(ac)
            else:
                self._mainloop.quit(
                    'Connection activation failed on {}: reason={}'.format(
                        ac.devname, ac.reason))

    @staticmethod
    def _is_connection_unavailable(err):
        return (isinstance(err, nmclient.GLib.GError) and
                err.domain == 'nm-manager-error-quark' and
                err.code == 2 and
                'is not available on the device' in err.message)

    def _get_activation_metadata(self):
        if self._nmdevice:
            activation_type = 'device'
            activation_object = self._nmdevice.get_iface()
        elif self._con_id:
            activation_type = 'connection_id'
            activation_object = self._con_id
        else:
            activation_type = activation_object = 'unknown'

        return activation_type, activation_object

    def _waitfor_active_connection_async(self, ac):
        ac.handlers.add(
            ac.nm_active_connection.connect(
                'state-changed', self._waitfor_active_connection_callback, ac)
        )

    def _waitfor_active_connection_callback(self,
                                            nm_act_con,
                                            state,
                                            reason,
                                            ac):
        ac.refresh_state()
        if ac.is_active:
            logging.debug(
                'Connection activation succeeded: dev=%s, con-state=%s',
                ac.devname, ac.state)
            for handler_id in ac.handlers:
                ac.nm_active_connection.handler_disconnect(handler_id)
            self._mainloop.execute_next_action()
        elif not ac.is_activating:
            for handler_id in ac.handlers:
                ac.nm_active_connection.handler_disconnect(handler_id)
            self._mainloop.quit(
                'Connection activation failed on {}: reason={}'.format(
                    ac.devname, ac.reason))

    @staticmethod
    def _add_connection_callback(src_object, result, user_data):
        mainloop = user_data
        try:
            con = src_object.add_connection_finish(result)
        except Exception as e:
            if mainloop.is_action_canceled(e):
                logging.debug(
                    'Connection adding canceled: error=%s', e)
            else:
                mainloop.quit(
                    'Connection adding failed: error={}'.format(e))
            return

        if con is None:
            mainloop.quit('Connection adding failed: error=unknown')
        else:
            devname = con.get_interface_name()
            logging.debug('Connection adding succeeded: dev=%s', devname)
            mainloop.execute_next_action()

    @staticmethod
    def _commit_changes_callback(src_object, result, user_data):
        mainloop, nmdev = user_data
        try:
            success = src_object.commit_changes_finish(result)
        except Exception as e:
            if mainloop.is_action_canceled(e):
                logging.debug('Connection update aborted: error=%s', e)
            else:
                mainloop.quit(
                    'Connection update failed: error={}, dev={}/{}'.format(
                        e, nmdev.props.interface, nmdev.props.state))
            return

        devname = src_object.get_interface_name()
        if success:
            logging.debug('Connection update succeeded: dev=%s', devname)
            mainloop.execute_next_action()
        else:
            mainloop.quit('Connection update failed: '
                          'dev={}, error=unknown'.format(devname))


class ConnectionSetting(object):

    def __init__(self, con_setting=None):
        self._setting = con_setting

    def create(self, con_name, iface_name, iface_type):
        con_setting = nmclient.NM.SettingConnection.new()
        con_setting.props.id = con_name
        con_setting.props.interface_name = iface_name
        con_setting.props.uuid = str(uuid.uuid4())
        con_setting.props.type = iface_type
        con_setting.props.autoconnect = True
        con_setting.props.autoconnect_slaves = (
            nmclient.NM.SettingConnectionAutoconnectSlaves.YES)

        self._setting = con_setting
        self._log_connection_info('ConnectionSetting.create')

    def import_by_profile(self, con_profile):
        base = con_profile.profile.get_setting_connection()
        new = nmclient.NM.SettingConnection.new()
        new.props.id = base.props.id
        new.props.interface_name = base.props.interface_name
        new.props.uuid = base.props.uuid
        new.props.type = base.props.type
        new.props.autoconnect = True
        new.props.autoconnect_slaves = base.props.autoconnect_slaves

        self._setting = new
        self._log_connection_info('ConnectionSetting.import_by_profile')

    def set_master(self, master, slave_type):
        if master is not None:
            self._setting.props.master = master
            self._setting.props.slave_type = slave_type

    @property
    def setting(self):
        return self._setting

    def _log_connection_info(self, source):
        setting = self._setting
        logging.debug(
            'Connection settings for %s:\n' +
            '\n'.join([
                'id: %s',
                'iface: %s',
                'uuid: %s',
                'type: %s',
                'autoconnect: %s',
                'autoconnect_slaves: %s'
            ]),
            source,
            setting.props.id,
            setting.props.interface_name,
            setting.props.uuid,
            setting.props.type,
            setting.props.autoconnect,
            setting.props.autoconnect_slaves
        )


def get_device_active_connection(nm_device):
    active_conn = None
    if nm_device:
        active_conn = nm_device.get_active_connection()
    return active_conn
