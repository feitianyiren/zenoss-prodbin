##############################################################################
# 
# Copyright (C) Zenoss, Inc. 2010, all rights reserved.
# 
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
# 
##############################################################################


import os
import shlex
import sys
import traceback
import subprocess
import signal
import time
from itertools import imap
from Products.ZenMessaging.audit import audit
from Products.ZenUI3.browser.streaming import StreamingView, StreamClosed
from Products.ZenUtils.jsonutils import unjson
from Products.Zuul import getFacade


class CommandView(StreamingView):
    """
    Accepts a POST request with a 'data' field containing JSON representing
    the command to be run and the uids of the devices against which to run it.

    Designed to work in concert with the Ext component Zenoss.CommandWindow.
    """
    def stream(self):
        data = unjson(self.request.get('data'))
        command = self.context.getUserCommands(asDict=True).get(data['command'], None)
        if command:
            for uid in data['uids']:
                target = self.context.unrestrictedTraverse(uid)
                self.execute(command, target)

    def _get_printable_command(self, raw_command, compiled_command, target):
        """
        Returns a string that do not contain zproperty values
        """
        printable_command = compiled_command

        zProps = []
        if hasattr(target, 'zenPropertyIds'):
            zProps = [zp for zp in target.zenPropertyIds() if zp in raw_command]

        if len(zProps) > 0:
            raw_items = raw_command.split()
            compiled_items = compiled_command.split()
            if len(raw_items) == len(compiled_items):
                printable_items = []
                for raw_item, compiled_item in zip(raw_items, compiled_items):
                    item = compiled_item
                    if any(p in raw_item for p in zProps):
                        item = raw_item
                    printable_items.append(item)
                printable_command = ' '.join(printable_items)
            else:
                # We could not filter the zprops so we return the raw command
                printable_command = raw_command

        return printable_command

    def execute(self, cmd, target):
        try:
            compiled = str(self.context.compile(cmd, target))

            timeout = getattr(target, 'zCommandUserCommandTimeout',
                              self.context.defaultTimeout)
            end = time.time() + timeout
            self.write('==== %s ====' % target.titleOrId())
            printable_command = self._get_printable_command(cmd.command, compiled, target)
            self.write(printable_command)

            audit('UI.Command.Invoke', cmd.id, target=target.id)
            p = subprocess.Popen(shlex.split(compiled),
                                 bufsize=1,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
            retcode = None
            while time.time() < end or retcode is not None:
                line = p.stdout.readline()
                if not line:
                    time.sleep(0.5)
                try:
                    self.write(line)
                except StreamClosed:
                    p.kill()
                    raise
                retcode = p.poll()
                if retcode is not None and not line:
                    # get anything else that is left
                    for line in p.stdout:
                        self.write(line)
                    break
            else:
                p.kill()
                self.write('Command timed out for %s (timeout is %s seconds)'%(
                                target.titleOrId(), timeout)
                          )
        except:
            self.write('Exception while performing command for %s' %
                       target.id)
            self.write('Type: %s   Value: %s' % tuple(sys.exc_info()[:2]))


class BackupView(StreamingView):
    def stream(self):
        data = unjson(self.request.get('data'))
        args = data['args']
        includeEvents = args[0]
        includeMysqlLogin = args[1]
        timeoutString = args[2]
        try:
            timeout = int(timeoutString)
        except ValueError:
            timeout = 120
        self.context.zport.dmd.manage_createBackup(includeEvents,
                includeMysqlLogin, timeout, self.request, self.write)


class TestDataSourceView(StreamingView):
    """
    Accepts a post with data in of the command to be tested against a device
    """

    def stream(self):
        """
        Called by the parent class, this method asks the datasource
        to test itself.
        """
        try:
            request = self.request
            data = unjson(request.form['data'])
            # datasource expect the request object, so set the attributes
            # from the request (so the user can test without saving the datasource)
            for key in data:
                request[key] = data[key]

            self.write("Preparing Command...")
            request['renderTemplate'] = False
            results = self.context.testDataSourceAgainstDevice(
                data.get('testDevice'),
                request,
                self.write,
                self.reportError)
            return results
        except Exception:
            self.write('Exception while performing command: <br />')
            self.write('<pre>%s</pre>' % (traceback.format_exc()))

    def reportError(self, title, body, priority=None, image=None):
        """
        If something goes wrong, just display it in the command output
        (as opposed to a browser message)
        """
        error = "<b>%s</b><p>%s</p>" % (title, body)
        return self.write(error)


class ModelView(StreamingView):
    """
    Accepts a list of uids to model.
    """
    def stream(self):
        data = unjson(self.request.get('data'))
        uids = data['uids']
        facade = getFacade('device', self.context)
        for device in imap(facade._getObject, uids):
            device.collectDevice(REQUEST=self.request, write=self.write)


class ModelDebugView(StreamingView):
    """
    Accepts a list of uids to model.
    """
    def stream(self):
        data = unjson(self.request.get('data'))
        uids = data['uids']
        facade = getFacade('device', self.context)
        for device in imap(facade._getObject, uids):
            device.collectDevice(REQUEST=self.request, write=self.write, debug=True)
