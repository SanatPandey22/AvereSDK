# Copyright (c) 2015-2017 Avere Systems, Inc.  All Rights Reserved.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
'''vFXT Cluster management

Cookbook/examples:

# A cluster is built with a service object (aws or gce)
service = vFXT.aws.Service() | vFXT.gce.Service()

# create a cluster
cluster = Cluster.create(service, ...)

# load from an existing, online cluster (queries xmlrpc)
cluster = Cluster.load(service, mgmt_ip='xxx', admin_password='xxx')

# offline with node instance ids provided
cluster = Cluster(service=service,
            nodes=['node-1', 'node-2', 'node-1'],
            admin_password='password',
            mgmt_ip='10.10.10.10')

serializeme = cluster.export()
cluster = Cluster(service, **serializeme)

cluster.start()
cluster.stop()
cluster.restart()
cluster.destroy()

cluster.shelve()
cluster.unshelve()

cluster.is_on()
cluster.is_off()
cluster.is_shelved()
cluster.status()

cluster.wait_for_healthcheck()
cluster.wait_for_service_checks()
cluster.wait_for_cluster_activity()
cluster.wait_for_nodes_to_join()

cluster_cfg = cluster.cluster_config()
joincfg = cluster.cluster_config(joining=True)

cluster.in_use_addresses()


rpc = cluster.xmlrpc()
cluster.verify_license()
cluster.upgrade('http://path/to/armada.pkg')

# buckets
cluster.make_test_bucket(bucketname='unique_bucket', corefiler='cloudfiler')
# or
service.create_bucket('unique_bucket')
cluster.attach_bucket('cloudfiler', 'mypassword', 'unique_bucket')
cluster.add_vserver('vserver')
cluster.add_vserver_junction('vserver','cloudfiler')

# NFS filer
cluster.attach_corefiler('grapnel', 'grapnel.lab.avere.net')
cluster.add_vserver_junction('vserver', 'grapnel', path='/nfs', export='/vol/woodwardj')

# maint
cluster.enable_ha()
cluster.rebalance_directory_managers()

cluster.refresh()
cluster.reload()


# Full AWS example
cluster = Cluster.create(aws, 'r3.2xlarge', 'mycluster', 'adminpass',
                        subnet='subnet-f99a618e',
                        placement_group='perf1',
                        wait_for_state='yellow')
try:
    cluster.make_test_bucket(bucketname='mycluster-bucket', corefiler='aws')
    cluster.add_vserver('vserver')
    cluster.add_vserver_junction('vserver', 'aws')
except Exception as e:
    cluster.destroy(remove_buckets=True)
    raise e


'''

import threading
import Queue
import time
import logging
import uuid
import re
import socket
from xmlrpclib import Fault as xmlrpclib_Fault
import math

import vFXT.xmlrpcClt
from vFXT.serviceInstance import ServiceInstance
from vFXT.service import *
from vFXT.cidr import Cidr

log = logging.getLogger(__name__)

class Cluster(object):
    '''Cluster representation

        Cluster composes the backend service object and performs all
        operations through it or the XMLRPC client.

    '''
    CONFIGURATION_EXPIRATION=7200
    LICENSE_TIMEOUT=120

    def __init__(self, service, **options):
        '''Constructor

            The only required argument is the service backend.

            To create a cluster, use Cluster.create()

            To load a cluster, use Cluster.load()

            Arguments:
                service: the backend service
                nodes ([], optional): optional list of node IDs
                mgmt_ip (str, optional): management address
                admin_password (str, optional): administration password
                name (str, optional): cluster name
                machine_type (str, optional): machine type of nodes in the cluster
                mgmt_netmask (str, optional): netmask of management network
                proxy_uri (str, optional): URI of proxy resource (e.g. http://user:pass@172.16.16.20:8080)

            If called with mgmt_ip and admin_password, the cluster object will
            query the management address and fill in all of the details required.

            If called with just a list of node IDs, the cluster will lookup the
            service instance backing objects associated with the node IDs.
            This is handy for offline clusters.
        '''
        self.service          = service
        self.nodes            = options.get('nodes',            [])
        self.mgmt_ip          = options.get('mgmt_ip',          None)
        self.admin_password   = options.get('admin_password',   None)
        self.name             = options.get('name',             None)
        self.machine_type     = options.get('machine_type',     None)

        self.mgmt_netmask     = options.get('mgmt_netmask',     None)
        self.cluster_ip_start = options.get('cluster_ip_start', None)
        self.cluster_ip_end   = options.get('cluster_ip_end',   None)
        self.proxy            = options.get('proxy_uri',        None)
        self.join_mgmt        = True
        self.trace_level      = None

        if self.proxy:
            self.proxy = validate_proxy(self.proxy) # imported from vFXT.service

        # we may be passed a list of instance IDs for offline clusters that we
        # can't query
        if self.service and self.nodes and all([not isinstance(i,ServiceInstance) for i in self.nodes]):
            instances = []
            for node_id in self.nodes:
                log.debug("Loading node {}".format(node_id))
                instance = service.get_instance(node_id)
                if not instance:
                    raise vFXTConfigurationException("Unable to find instance {}".format(node_id))
                instances.append(ServiceInstance(service=self.service, instance=instance))
            self.nodes = instances

        if self.mgmt_ip and self.admin_password and self.nodes and self.is_on():
            # might as well if we can, otherwise use the load() constructor
            self.load_cluster_information()

    @classmethod
    def create(cls, service, machine_type, name, admin_password, **options):
        '''Create a cluster

            Arguments:
                service: the backend service
                machine_type (str): service specific machine type
                name (str): cluster name (used or all subsequent resource naming)
                admin_password (str): administration password to assign to the cluster
                wait_for_state (str, optional): red, yellow, green cluster state (defaults to yellow)
                proxy_uri (str, optional): URI of proxy resource (e.g. http://user:pass@172.16.16.20:8080)
                skip_cleanup (bool, optional): do not clean up on failure
                management_address (str, optional): management address for the cluster
                trace_level (str, optional): trace configuration
                join_instance_address (bool=False): Join cluster using instance rather than management address
                size (int, optional): size of cluster (node count)
                root_image (str, optional): root disk image name
                skip_cleanup (bool, optional): do not clean up on failure
                address_range_start (str, optional): The first of a custom range of addresses to use for the cluster
                address_range_end (str, optional): The last of a custom range of addresses to use for the cluster
                address_range_netmask (str, optional): cluster address range netmask
                **options: passed to Service.create_cluster()
        '''

        c                 = cls(service)
        c.admin_password  = admin_password or '' # could be empty
        c.machine_type    = machine_type
        c.name            = name
        c.proxy           = options.get('proxy_uri', None)
        c.trace_level     = options.get('trace_level', None)
        c.join_mgmt       = False if options.get('join_instance_address', False) else True

        if c.proxy:
            c.proxy = validate_proxy(c.proxy) # imported from vFXT.service

        if not name:
            raise vFXTConfigurationException("A cluster name is required")
        if not cls.valid_cluster_name(name):
            raise vFXTConfigurationException("{} is not a valid cluster name".format(name))
        if options.get('management_address'):
            requested_mgmt_ip = options.get('management_address')
            if service.in_use_addresses('{}/32'.format(requested_mgmt_ip)):
                raise vFXTConfigurationException("The requested management address {} is already in use".format(requested_mgmt_ip))

        # machine type is validated by service create_cluster

        try:
            service.create_cluster(c, **options)
            if options.get('skip_configuration'):
                return
        except KeyboardInterrupt:
            if not options.get('skip_cleanup', False):
                c.destroy()
            raise

        # any service specific instance checks should happen here... the checks
        # might have to restart the nodes
        try:
            c.wait_for_service_checks()
        except (KeyboardInterrupt, Exception) as e:
            log.error('Failed to wait for service checks: {}'.format(e))
            if not options.get('skip_cleanup', False):
                c.destroy()
            else:
                c.telemetry()
            raise

        # should get all the nodes joined by now
        try:
            retries = int(options.get('join_wait', 180+(180*math.log(len(c.nodes)))))
            c.wait_for_nodes_to_join(retries=retries)
        except (KeyboardInterrupt, Exception) as e:
            log.error('Failed to wait for nodes to join: {}'.format(e))
            if not options.get('skip_cleanup', False):
                c.destroy()
            else:
                c.telemetry()
            raise

        try:
            log.info("Waiting for cluster healthcheck")
            c.wait_for_healthcheck(state=options.get('wait_for_state', 'yellow'),
                duration=int(options.get('wait_for_state_duration', 30)))

            # disable node join
            c.allow_node_join(False)
            # rename nodes
            c.set_node_naming_policy()

            if len(c.nodes) > 1:
                c.enable_ha()
        except (KeyboardInterrupt, Exception) as e:
            log.error("Cluster failed: {}".format(e))
            if not options.get('skip_cleanup', False):
                c.destroy()
            else:
                c.telemetry()
            raise vFXTCreateFailure(e)

        return c

    def wait_for_healthcheck(self, state='green', retries=ServiceBase.WAIT_FOR_HEALTH_CHECKS, duration=1, conn_retries=1):
        '''Poll for cluster maxConditions
            This requires the cluster to be on and be accessible via RPC

            Arguments:
                state (str='green'): red, yellow, green
                retries (int, optional): number of retries
                duration (int, optional): number of consecutive seconds condition was observed
                conn_retries (int, optional): number of connection retries

            Sleeps Service.POLLTIME between each retry.
        '''
        retries      = int(retries)
        conn_retries = int(conn_retries)
        duration     = int(duration)
        log.debug("Waiting for healthcheck state {} for duration {}".format(state, duration))
        xmlrpc = self.xmlrpc(conn_retries) #pylint: disable=unused-variable

        start_time = int(time.time())
        observed = 0 # observed time in the requested state

        # cluster health check
        acceptable_states = [state, 'green']
        if state == 'red':
            acceptable_states.append('yellow')
        while True:
            alertstats = {}
            try:
                alertstats = self.xmlrpc().cluster.maxActiveAlertSeverity()
            except Exception as e:
                log.debug("Ignoring cluster.maxActiveAlertSeverity() failure: {}".format(e))

            if 'maxCondition' in alertstats and alertstats['maxCondition'] in acceptable_states:
                observed = int(time.time()) - start_time
                if observed >= duration:
                    log.debug("{} for {}s({})... alertStats: {}".format(state, duration, observed, alertstats))
                    break
            else:
                observed = 0
                start_time = int(time.time())

            if retries % 10 == 0:
                log.debug("Not {} for {}s({})... alertStats: {}".format(state, duration, observed, alertstats))

            retries -= 1
            if retries == 0:
                conditions  = self.xmlrpc().alert.conditions()
                alert_codes = [c['name'] for c in conditions if c['severity'] != state]
                if alert_codes:
                    raise vFXTStatusFailure("Healthcheck for state {} failed: {}".format(state, alert_codes))
                else:
                    raise vFXTStatusFailure("Healthcheck for state {} failed".format(state))
            self._sleep()

    @classmethod
    def load(cls, service, mgmt_ip, admin_password):
        '''Load an existing cluster over RPC

            Arguments:
                mgmt_ip (str): management address
                admin_password (str): administration password
        '''
        cluster                 = cls(service)
        cluster.mgmt_ip         = mgmt_ip
        cluster.admin_password  = admin_password

        cluster.load_cluster_information()
        return cluster

    def load_cluster_information(self):
        '''Load cluster information through XMLRPC and the service backend

            Raises: vFXTConfigurationException
        '''
        log.debug("Connecting to {} to load cluster data".format(self.mgmt_ip))
        xmlrpc          = self.xmlrpc()
        cluster_data    = xmlrpc.cluster.get()
        self.name       = cluster_data['name']
        self.mgmt_netmask = cluster_data['mgmtIP']['netmask']
        expected_count  = len(xmlrpc.node.list())

        log.debug("Loading {} nodes".format(self.name))
        self.service.load_cluster_information(self)
        if not self.nodes:
            raise vFXTConfigurationException("No nodes found for cluster")

        found_count = len(self.nodes)
        if expected_count != found_count:
            raise vFXTStatusFailure("Failed to load all {} nodes (found {})".format(expected_count, found_count))

    def cluster_config(self, joining=False, expiration=CONFIGURATION_EXPIRATION):
        '''Return cluster configuration for master and slave nodes

            Arguments:
                joining (bool=False): configuration for a joining node
                expiration (int, optional): configuration expiration for a joining node

            Raises: vFXTConfigurationException
        '''
        expiry = str(int(time.time()+(expiration or self.CONFIGURATION_EXPIRATION)))

        if joining:
            mgmt_ip = (self.nodes[0].ip() if self.nodes and not self.join_mgmt else self.mgmt_ip)
            return '# cluster.cfg\n[basic]\njoin cluster={}\nexpiration={}\n'.format(mgmt_ip, expiry)

        dns_servs   = self.service.get_dns_servers()
        ntp_servs   = self.service.get_ntp_servers()
        router      = self.service.get_default_router()

        if not all([self.mgmt_ip, self.mgmt_netmask,self.cluster_ip_start, self.cluster_ip_end]):
            raise vFXTConfigurationException("Management IP/Mask and the cluster IP range is required")

        # generate config
        config = '''# cluster.cfg''' \
                 '''\n[basic]''' \
                 '''\ncluster name={}''' \
                 '''\npassword={}''' \
                 '''\nexpiration={}''' \
                 '''\n[management network]''' \
                 '''\naddress={}''' \
                 '''\nnetmask={}''' \
                 '''\ndefault router={}''' \
                 '''\n[cluster network]''' \
                 '''\nfirst address={}''' \
                 '''\nlast address={}'''  \
                .format(self.name,
                        self.admin_password,
                        expiry,
                        self.mgmt_ip,
                        self.mgmt_netmask,
                        router,
                        self.cluster_ip_start,
                        self.cluster_ip_end)

        config += '\n[dns]\n'
        dns_count = len(dns_servs)
        for idx in range(3):
            v = dns_servs[idx] if idx < dns_count else ''
            config += 'server{}={}\n'.format(idx+1, v)
        config += 'domain=\n'

        config += '\n[ntp]\n'
        ntp_count = len(ntp_servs)
        for idx in range(3):
            v = ntp_servs[idx] if idx < ntp_count else ''
            config += 'server{}={}\n'.format(idx+1, v)

        return config

    def verify_license(self, wait=LICENSE_TIMEOUT):
        '''Verify a license has been provisioned for the cluster

            Arguments:
                wait (int): time to wait in seconds for the license provisioning (default 60)

            Raises: vFXTConfigurationException
        '''
        log.info('Waiting for FlashCloud licensing feature')
        while wait > 0:
            try:
                licenses = self.xmlrpc().cluster.listLicenses()
                if 'FlashCloud' in licenses['features']:
                    log.info('Feature FlashCloud enabled.')
                    return
            except Exception as e:
                log.debug(e)
            if wait % 10 == 0:
                log.debug('Waiting for the FlashCloud license feature to become enabled')
            wait -= 1
            self._sleep()

        raise vFXTConfigurationException("Unable to verify cluster licensing")

    def xmlrpc(self, retries=1, password=None):
        '''Connect and return a new RPC connection object

            Arguments:
                retries (int, optional): number of retries
                password (str, optional): defaults to the cluster admin_password

            Raises: vFXTConnectionFailure
        '''
        if not self.mgmt_ip:
            raise vFXTConnectionFailure("Unable to make remote API connection without a management address")
        addrs = [self.mgmt_ip]
        if self.nodes:
            addrs.append(self.nodes[0].ip())

        password = password if password else self.admin_password
        if not password:
            raise vFXTConnectionFailure("Unable to make remote API connection without a password")

        while True:
            # try our mgmt address or the first nodes instance address
            for addr in addrs:
                try:
                    xmlrpc = vFXT.xmlrpcClt.getXmlrpcClient("https://{}/cgi-bin/rpc2.py".format(addr), do_cert_checks=False)
                    xmlrpc.system.login( "admin".encode('base64'), self.admin_password.encode('base64') )
                    if addr != self.mgmt_ip:
                        log.warn("Connected via instance address {} instead of management address {}".format(addr, self.mgmt_ip))
                        self._log_conditions(xmlrpc)
                    return xmlrpc
                except Exception as e:
                    log.debug("Retrying failed XMLRPC connection to {}: {}".format(addr, e))
                    if retries == 0:
                        raise vFXTConnectionFailure("Failed to make remote API connection: {}".format(e))
            retries -= 1
            self._sleep()

    @classmethod
    def _log_conditions(cls, xmlrpc_handle):
        '''Warn the condition names, debug log the full conditions

            This is useful when we are polling and want to show what is going
            on with the cluster while we wait.
        '''
        try:
            conditions = xmlrpc_handle.alert.conditions()
            if conditions:
                condition_names = [_['name'] for _ in conditions]
                log.warn("Current conditions: {}".format(', '.join(condition_names)))
                log.debug(conditions)
        except Exception as e:
            log.debug("Failed to get condition list: {}".format(e))

    def telemetry(self, nowait=True):
        '''Kick off a minimal telemetry reporting

            Arguments:
                nowait (bool=True): wait until complete

            Raises vFXTStatusFailure on failure while waiting.
        '''
        try:
            log.info("Kicking off minimal telemetry reporting.")
            response = self.xmlrpc().support.executeNormalMode('cluster', 'gsimin')
            log.debug('gsimin response {}'.format(response))
            if nowait:
                return
            if response != 'success':
                while True:
                    is_done = self.xmlrpc().support.taskIsDone(response) # returns bool
                    if is_done:
                        break
                    self._sleep()
        except Exception as e:
            log.debug("Telemetry failed: {}".format(e))
            raise vFXTStatusFailure('Telemetry failed: {}'.format(e))

    def upgrade(self, upgrade_url, retries=ServiceBase.WAIT_FOR_HEALTH_CHECKS):
        '''Upgrade a cluster from the provided URL

            Arguments:
                upgrade_url (str): URL for armada package

            Raises: vFXTConnectionFailure
        '''
        cluster     = self.xmlrpc().cluster.get()
        alt_image   = cluster['alternateImage']

        if not self.xmlrpc().cluster.upgradeStatus()['allowDownload']:
            raise vFXTConfigurationException("Upgrade downloads are not allowed at this time")

        # note any existing activities to skip
        existing_activities = [a['id'] for a in self.xmlrpc().cluster.listActivities()]

        log.info("Fetching alternate image from {}".format(upgrade_url))
        response = self.xmlrpc().cluster.upgrade(upgrade_url)
        if response != 'success':
            raise vFXTConfigurationException("Failed to start upgrade download: {}".format(response))

        op_retries = retries
        while cluster['alternateImage'] == alt_image:
            self._sleep()
            try:
                cluster    = self.xmlrpc().cluster.get()
                activities = [act for act in self.xmlrpc().cluster.listActivities()
                                if act['id'] not in existing_activities # skip existing
                                if act['process'] == 'Cluster upgrade' # look for cluster upgrade or download
                                or 'software download' in act['process']]
                if 'failure' in [a['state'] for a in activities]:
                    raise vFXTConfigurationException("Failed to download upgrade image")
                if op_retries % 10 == 0:
                    log.debug('Current activities: {}'.format(', '.join([act['status'] for act in activities])))

                # check for double+ upgrade to same version
                existing_ver_msg = 'Download {} complete'.format(alt_image)
                if existing_ver_msg in [act['status'] for act in activities]:
                    log.debug("Redownloaded existing version")
                    break

            except vFXTConfigurationException as e:
                log.debug(e)
                raise
            except Exception as e:
                if op_retries % 10 == 0:
                    log.debug("Retrying install check: {}".format(e))
            op_retries -= 1
            if op_retries == 0:
                raise vFXTConnectionFailure("Timeout waiting for alternate image")

        cluster = self.xmlrpc().cluster.get()
        alt_image = cluster['alternateImage']
        if cluster['alternateImage'] == cluster['activeImage']:
            log.info("Skipping upgrade since this version is active")
            return

        log.debug("Waiting for alternateImage to settle (FIXME)...")
        self._sleep(15) # time to settle?
        # instead we should be able to use self.xmlrpc().cluster.upgradeStatus()['allowActivate']

        log.info("Activating alternate image")
        response = self.xmlrpc().cluster.activateAltImage()
        log.debug("activateAltImage response: {}".format(response))

        existing_activities = [a['id'] for a in self.xmlrpc().cluster.listActivities()]
        log.debug("existing activities prior to upgrade: {}".format(existing_activities))
        op_retries = retries
        while cluster['activeImage'] != alt_image:
            log.debug("activeImage {} != {}".format(cluster['activeImage'],alt_image))
            self._sleep()
            try:
                log.debug("getting cluster info")
                cluster    = self.xmlrpc().cluster.get()
                log.debug("getting activities")
                activities = [act for act in self.xmlrpc().cluster.listActivities()
                                if act['id'] not in existing_activities # skip existing
                                if act['process'] == 'Cluster upgrade' # look for cluster upgrade or activate
                                or 'software activate' in act['process']]
                if 'failed' in [a['state'] for a in activities]:
                    raise vFXTConfigurationException("Failed to activate alternate image")
                if op_retries % 10 == 0:
                    log.debug('Current activities: {}'.format(', '.join([act['status'] for act in activities])))
            except vFXTConfigurationException as e:
                log.debug(e)
                raise
            except Exception as e:
                log.debug("Retrying upgrade check: {}".format(e))
            op_retries -= 1
            log.debug("op_retries is now {}".format(op_retries))
            if op_retries == 0:
                raise vFXTConnectionFailure("Timeout waiting for active image")

        log.info(self.xmlrpc().cluster.upgradeStatus()['status'])

    def add_nodes(self, count=1, **options):
        '''Add nodes to the cluster

            This extends the address ranges of the cluster and all configured
            vservers (if required) to accommodate the new nodes.

            Arguments:
                count (int, optional): number of nodes to add
                skip_cleanup (bool, optional): do not clean up on failure
                join_wait (int, optional): join wait time (defaults to wait_for_nodes_to_join default)
                address_range_start (str, optional): Specify the first of a custom range of addresses to use
                address_range_end (str, optional): Specify the last of a custom range of addresses to use
                address_range_netmask (str, optional): Specify the netmask of the custom address range to use
                **options: options to pass to the service backend

            Raises: vFXTCreateFailure


            On failure, undoes cluster and vserver configuration changes.
        '''
        self.reload()
        log.info("Extending cluster {} by {}".format(self.name, count))

        node_count = len(self.nodes)
        if not node_count:
            raise vFXTConfigurationException("Cannot add a node to an empty cluster")

        self.service._add_cluster_nodes_setup(self, count, **options)

        # check to see if we can add nodes with the current licensing information
        license_data     = self.xmlrpc().cluster.listLicenses()
        licensed_count   = int(license_data['maxNodes'])
        if (node_count+count) > licensed_count:
            msg = "Cannot expand cluster to {} nodes as the current licensed maximum is {}"
            raise vFXTConfigurationException(msg.format(node_count+count, licensed_count))

        cluster_data    = self.xmlrpc().cluster.get()
        cluster_ips_per_node = int(cluster_data['clusterIPNumPerNode'])
        vserver_count    = len(self.xmlrpc().vserver.list())
        existing_vserver = self.in_use_addresses('vserver')
        existing_cluster = self.in_use_addresses('cluster')
        need_vserver     = ((node_count+count)*vserver_count) - len(existing_vserver)
        need_cluster     = ((node_count+count)*cluster_ips_per_node) - len(existing_cluster)
        need_cluster     = need_cluster if need_cluster > 0 else 0
        need_vserver     = need_vserver if need_vserver > 0 else 0
        need_private     = count if self.service.ALLOCATE_PRIVATE_ADDRESSES else 0

        added = [] # cluster and vserver extensions (for undo)

        ip_count = need_vserver + need_cluster + need_private

        if ip_count > 0: # if we need more, extend ourselves
            in_use_addrs    = self.in_use_addresses()

            custom_ip_config_reqs = ['address_range_start', 'address_range_end', 'address_range_netmask']
            if all([options.get(_) for _ in custom_ip_config_reqs]):
                avail_ip = Cidr.expand_address_range(options.get('address_range_start'), options.get('address_range_end'))
                mask = options.get('address_range_netmask')
                if len(avail_ip) < ip_count:
                    raise vFXTConfigurationException("Not enough addresses provided, require {}".format(ip_count))
            else:
                avail_ips, mask = self.service.get_available_addresses(count=ip_count, contiguous=True, in_use=in_use_addrs)

            if need_private:
                options['private_addresses']  = avail_ips[0:need_private]
                del avail_ips[0:need_private]

            if need_cluster > 0:
                addresses = avail_ips[0:need_cluster]
                del avail_ips[0:need_cluster]
                body      = {'firstIP':addresses[0],'netmask':mask,'lastIP':addresses[-1]}
                log.info("Extending cluster address range by {}".format(need_cluster))
                log.debug("{}".format(body))
                activity = self.xmlrpc().cluster.addClusterIPs(body)
                if activity != 'success':
                    while True:
                        response = self.xmlrpc().cluster.getActivity(activity)
                        if 'state' in response and response['state'] == 'success':
                            break
                        if 'status' in response and response['status'] == 'failure':
                            raise vFXTConfigurationException('Failed to extend cluster addresses')
                        self._sleep()
                added.append({'cluster':body})

            if need_vserver > 0:
                for vserver in self.xmlrpc().vserver.list():
                    v_len     = len([a for r in self.xmlrpc().vserver.get(vserver)[vserver]['clientFacingIPs']
                                for a in xrange(Cidr.from_address(r['firstIP']), Cidr.from_address(r['lastIP'])+1)])
                    to_add    = (node_count+count) - v_len
                    if to_add < 1:
                        continue

                    addresses = avail_ips[0:to_add]
                    del avail_ips[0:to_add]
                    body      = {'firstIP':addresses[0],'netmask':mask,'lastIP':addresses[-1]}
                    log.info("Extending vserver {} address range by {}".format(vserver, need_vserver))
                    log.debug("{}".format(body))
                    activity = self.xmlrpc().vserver.addClientIPs(vserver, body)
                    if activity != 'success':
                        while True:
                            response = self.xmlrpc().cluster.getActivity(activity)
                            if 'state' in response and response['state'] == 'success':
                                break
                            if 'status' in response and response['status'] == 'failure':
                                raise vFXTConfigurationException('Failed to extend vserver {} addresses'.format(vserver))
                            self._sleep()
                    added.append({'vserver':body})

        # now add the node(s)
        try:
            self.allow_node_join()
            self.service.add_cluster_nodes(self, count, **options)
            self.wait_for_service_checks()

            # book keeping... may have to wait for a node to update image
            wait = int(options.get('join_wait', 500+(500*math.log(count))))
            self.wait_for_nodes_to_join(retries=wait)
            self.refresh()
            self.enable_ha()
            self.allow_node_join(False)
            self.set_node_naming_policy()
        except (KeyboardInterrupt, Exception) as e:
            log.error(e)
            if options.get('skip_cleanup', False):
                self.telemetry()
                raise vFXTCreateFailure(e)

            log.info("Undoing configuration changes for node addition")

            # our current list
            expected_nodes = [n.id() for n in self.nodes]
            # refresh and get what the cluster sees
            self.service.load_cluster_information(self)
            joined_nodes = [n.id() for n in self.nodes]
            # find the difference
            unjoined = list(set(expected_nodes)^set(joined_nodes))
            unjoined_nodes = [ServiceInstance(self.service, i) for i in unjoined]
            # exclude those in the middle of joining
            joining_node_addresses = [_['address'] for _ in self.xmlrpc().node.listUnconfiguredNodes() if 'joining' in _['status']]
            unjoined_nodes = [_ for _ in unjoined_nodes if _.ip() not in joining_node_addresses]
            # destroy the difference
            if unjoined_nodes:
                try:
                    self.parallel_call(unjoined_nodes, 'destroy')
                except Exception as destroy_e:
                    log.error('Failed to undo configuration: {}'.format(destroy_e))

            # if we added no nodes successfully, clean up addresses added
            if len(unjoined) == count:
                for a in added:
                    if 'vserver' in a:
                        a = a['vserver']
                        for vserver in self.xmlrpc().vserver.list():
                            for r in self.xmlrpc().vserver.get(vserver)[vserver]['clientFacingIPs']:
                                if r['firstIP'] == a['firstIP'] and r['lastIP'] == a['lastIP']:
                                    log.debug("Removing vserver range {}".format(r))
                                    activity = self.xmlrpc().vserver.removeClientIPs(vserver, r['name'])
                                    if activity != 'success':
                                        while True:
                                            response = self.xmlrpc().cluster.getActivity(activity)
                                            if 'state' in response and response['state'] == 'success':
                                                break
                                            if 'status' in response and response['status'] == 'failure':
                                                log.error('Failed to undo vserver extension')
                                                break
                                            self._sleep()
                    if 'cluster' in a:
                        a = a['cluster']
                        for r in self.xmlrpc().cluster.get()['clusterIPs']:
                            if r['firstIP'] == a['firstIP'] and r['lastIP'] == a['lastIP']:
                                log.debug("Removing cluster range {}".format(r))
                                activity = self.xmlrpc().cluster.removeClusterIPs(r['name'])
                                if activity != 'success':
                                    while True:
                                        response = self.xmlrpc().cluster.getActivity(activity)
                                        if 'state' in response and response['state'] == 'success':
                                            break
                                        if 'status' in response and response['status'] == 'failure':
                                            log.error('Failed to undo cluster extension')
                                            break
                                        self._sleep()

            self.allow_node_join(False)
            raise vFXTCreateFailure(e)

    def parallel_call(self, serviceinstances, method, **options):
        '''Run the named method across all nodes

            A thread is spawned to run the method for each instance.

            Arguments:
                serviceinstances [ServiceInstance]: list of ServiceInstance objects
                method (str): method to call on each ServiceInstance

            Raises: vFXTServiceFailure
        '''
        threads = []
        failq   = Queue.Queue()

        def thread_cb(service, instance_id, q):
            '''thread callback'''
            try:
                # create this within the thread
                instance = ServiceInstance(service=service, instance_id=instance_id)
                instance.__getattribute__(method)(**options)
            except Exception as e:
                log.error("Failed to {} {}: {}".format(method, instance_id,e))
                q.put(("Failed to {} instance {}".format(method, instance_id),e))

        for si in serviceinstances:
            t = threading.Thread(target=thread_cb, args=(si.service, si.instance_id, failq,))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        failed = []
        while True:
            try:
                failed.append(failq.get_nowait())
            except Queue.Empty:
                break

        if failed:
            raise vFXTServiceFailure(failed)

    def start(self):
        '''Start all nodes in the cluster'''
        self.parallel_call(self.nodes, 'start')
        self.refresh()

    def can_stop(self):
        '''Some configurations cannot be stopped. Check if this is one.
        '''
        return all([_.can_stop() for _ in self.nodes])

    def stop(self, clean_stop=True, retries=ServiceBase.WAIT_FOR_STOP):
        '''Stop all nodes in the cluster

            Arguments:
                clean_stop (bool, optional): Issues cluster powerdown first (defaults to True)
                retries (int, optional): number of retries (default 600)
        '''

        # we might be only a collection of nodes... make sure we have mgmt ip,
        # password, etc... if so we power down the cluster before calling the
        # service backend stop.
        if clean_stop and (self.admin_password and self.nodes and self.is_on()):

            # if we don't have the mgmt ip, use node1
            if not self.mgmt_ip:
                self.mgmt_ip = self.nodes[0].ip()

            if not all([_.can_stop() for _ in self.nodes]):
                raise vFXTConfigurationException("Node configuration prevents them from being stopped")

            log.info("Powering down the cluster")
            rpc_retries = self.service.XMLRPC_RETRIES
            while True:
                response = self.xmlrpc().cluster.powerdown()
                log.debug('cluster.powerdown returned {}'.format(response))
                if response == 'success':
                    break
                rpc_retries -= 1
                if rpc_retries == 0:
                    raise vFXTStatusFailure("Failed to power down the cluster")

            log.info("Waiting for cluster to go offline")
            while self.is_on():
                self._sleep()
                self.refresh()
                retries -= 1
                if retries == 0:
                    raise vFXTStatusFailure("Timed out waiting for the cluster to go offline")


        self.parallel_call(self.nodes, 'stop')
        self.refresh()

    def restart(self):
        '''Calls stop and then start'''
        self.stop()
        self.start()

    def destroy(self, remove_buckets=False, **options):
        '''Destroy the cluster

            Arguments:
                remove_buckets (bool=False): EXPERIMENTAL bucket removal

                **options: passed to ServiceInstance.destroy()
        '''
        buckets = []
        if remove_buckets:
            try:
                buckets = [d['bucket'] for c in [self.xmlrpc().corefiler.get(cf) \
                            for cf in self.xmlrpc().corefiler.list()] \
                            for d in c.values() \
                            if 'bucket' in d and d['s3Type'] == self.service.S3TYPE_NAME]
            except Exception as e:
                log.debug("Failed to lookup buckets: {}".format(e))

        self.parallel_call(self.nodes, 'destroy', **options)
        if remove_buckets and buckets:
            for bucket_name in buckets:
                log.debug("Deleting bucket {}".format(bucket_name))
                try:
                    # TODO will fail as non-empty
                    self.service.delete_bucket(bucket_name)
                except Exception as e:
                    log.debug("Ignoring remove bucket failure: {}".format(e))
        # any post destroy cleanup activities that may be remaining
        self.service.post_destroy_cluster(self)

    def shelve(self, **options):
        '''Shelve all nodes in the cluster'''

        # if we can make rpc calls, try to use maint.setShelve()
        if not self.admin_password or not (self.nodes and self.is_on()):
            raise vFXTConfigurationException('Unable to shelve cluster without xmlrpc connectivity')

        # if we don't have the mgmt ip, use node1
        if not self.mgmt_ip:
            self.mgmt_ip = self.nodes[0].ip()

        if not all([_.can_shelve() for _ in self.nodes]):
            raise vFXTConfigurationException("Node configuration prevents them from being shelved")

        try:
            xmlrpc = self.xmlrpc()

            response = xmlrpc.system.enableAPI('maintenance')
            if response != 'success':
                raise vFXTConfigurationException('Failed to enable maintenance API')

            response = xmlrpc.maint.setShelve()
            if response != 'success':
                raise vFXTConfigurationException('Failed to notify cluster of intent to shelve')
            log.debug('Called maint.setShelve()')
        except xmlrpclib_Fault as e:
            if int(e.faultCode) != 108: # Method maint.setShelve not supported
                raise
            log.debug('maint.setShelve not supported in this release')

        # XXX need to flush the cache to the backend prior if shelving (add flush bool opt)

        self.stop(clean_stop=options.get('clean_stop', True))
        self.parallel_call(self.nodes, 'shelve', **options)
        self.refresh()

    def unshelve(self, **options):
        '''Unshelve all nodes in the cluster'''
        self.parallel_call(self.nodes, 'unshelve', **options)
        self.refresh()
        # we might be only a collection of nodes... make sure we have mgmt ip,
        # password, etc... if so we wait at least until we have api connectivity
        if self.mgmt_ip and self.admin_password and self.nodes and self.is_on():
            self.wait_for_healthcheck(state='red', duration=1, conn_retries=20)

    def is_on(self):
        '''Returns true if all nodes are on'''
        if self.nodes:
            return all(i.is_on() for i in self.nodes)
        return False

    def is_off(self):
        '''Returns true if all nodes are off'''
        if self.nodes:
            return all(i.is_off() for i in self.nodes)
        return False

    def is_shelved(self):
        '''Returns true if all nodes are shelved'''
        if self.is_off():
            return all([n.is_shelved() for n in self.nodes])
        else:
            return False

    def status(self):
        '''Returns a list of node id:status'''
        return [{n.id(): n.status() } for n in self.nodes]

    def wait_for_service_checks(self):
        '''Wait for Service checks to complete for all nodes

            This may not be available for all backends and thus may be a noop.
        '''
        self.parallel_call(self.nodes, 'wait_for_service_checks')

    def make_test_bucket(self, bucketname=None, corefiler=None, proxy=None, remove_on_fail=False, **options):
        '''Create a test bucket for the cluster

            Convenience wrapper function for testing.  Calls create_bucket()
            and then attach_bucket().

            Arguments:
                bucketname (str, optional): name of bucket or one is generated
                corefiler (str, optional): name of corefiler or bucketname
                proxy (str, optional): proxy configuration to use
                remove_on_fail (bool, optional): remove the corefiler if the configuration does not finish

            Returns:
                key (dict): encryption key for the bucket as returned from attach_bucket
        '''
        bucketname      = bucketname or "{}-{}".format(self.name, str(uuid.uuid4()).lower().replace('-',''))[0:63]
        corefiler       = corefiler or bucketname
        self.service.create_bucket(bucketname)
        log.info("Created bucket {} ".format(bucketname))
        return self.attach_bucket(corefiler, bucketname, master_password=self.admin_password, proxy=proxy, remove_on_fail=remove_on_fail, **options)

    def attach_bucket(self, corefiler, bucketname, master_password=None, credential=None, proxy=None, **options):
        '''Attach a named bucket as core filer

            Arguments:
                corefiler (str): name of the corefiler to create
                bucketname (str): name of existing bucket to attach
                master_password (str, optional): otherwise cluster admin password is used
                credential (str, optional): cloud credential or one is created or reused by the backing service
                proxy (str, optional): proxy configuration to use

                type (str, optional): type of corefiler (default 'cloud')
                cloud_type (str, optional): cloud type (default 's3')
                s3_type (str, optional): S3 type (default Service.S3TYPE_NAME)
                https (str, optional): use HTTPS (default 'yes')
                crypto_mode (str, optional): crypto mode (default CBC-AES-256-HMAC-SHA-512)
                compress_mode (str, optional): compression mode (default LZ4)
                remove_on_fail (bool, optional): remove the corefiler if the configuration does not finish
                existing_data (bool, optional): the bucket has existing data in it (defaults to False)

            Returns:
                key (dict): encryption key for the bucket

            Raises: vFXTConfigurationException
        '''
        if corefiler in self.xmlrpc().corefiler.list():
            raise vFXTConfigurationException("Corefiler {} exists".format(corefiler))

        if not credential:
            log.debug("Looking up credential as none was specified")
            credential = self.service.authorize_bucket(self, bucketname)
            log.debug("Using credential {}".format(credential))

        if not master_password:
            master_password = self.admin_password

        # set proxy if provided
        if not proxy:
            if self.proxy:
                proxy = self.proxy.hostname

        data = {
            'type': options.get('type') or 'cloud',
            'cloudType': options.get('cloud_type') or 's3',
            's3Type': options.get('s3_type') or self.service.S3TYPE_NAME,
            'bucket': bucketname,
            'cloudCredential': credential,
            'https': options.get('https') or 'yes',
            'compressMode': options.get('compress_mode') or 'LZ4',
            'cryptoMode': options.get('crypto_mode') or 'CBC-AES-256-HMAC-SHA-512',
            'proxy': proxy or '',
            'bucketContents': 'used' if options.get('existing_data', False) else 'empty',
        }

        log.info("Creating corefiler {}".format(corefiler))
        log.debug("corefiler.createCloudFiler options {}".format(data))

        activity = None
        retries = self.LICENSE_TIMEOUT
        while True:
            try:
                activity = self.xmlrpc().corefiler.createCloudFiler(corefiler, data)
                break
            except xmlrpclib_Fault as e:
                # This cluster is not licensed for cloud core filers.  A FlashCloud license is required.
                err_msg = 'A FlashCloud license is required'
                if not (int(e.faultCode) == 100 and err_msg in e.faultString):
                    raise
                log.debug("Waiting for error to clear: {}".format(e))
                if retries == 0:
                    raise
                retries -= 1
                self._sleep()

        retries = self.service.WAIT_FOR_SUCCESS
        if activity != 'success':
            while True:
                response = {}
                try:
                    response = self.xmlrpc().cluster.getActivity(activity)
                except Exception as e:
                    log.debug("Failed to get activity {}: {}".format(activity, e))

                if 'state' in response:
                    if response['state'] == 'success':
                        break
                    if response['state'] == 'failure':
                        raise vFXTConfigurationException('Failed to create corefiler {}: {}'.format(corefiler, response.get('status', 'Unknown')))
                if retries == 0:
                    raise vFXTConfigurationException('Giving up: {}'.format(response['status']))
                retries -= 1
                if retries % 10 == 0 and 'status' in response:
                    log.info(response['status'])
                    self._log_conditions(self.xmlrpc())
                self._sleep()
        if corefiler not in self.xmlrpc().corefiler.list():
            raise vFXTConfigurationException('Failed to create corefiler {}: Not found'.format(corefiler))

        key = {}
        if options.get('crypto_mode') != 'DISABLED':
            log.info("Generating master key for {}".format(corefiler))
            retries = self.service.XMLRPC_RETRIES
            while True:
                try:
                    key = self.xmlrpc().corefiler.generateMasterKey(corefiler, master_password)
                    if 'keyId' in key and 'recoveryFile' in key:
                        break
                except Exception as e:
                    log.debug(e)
                    if retries == 0:
                        raise vFXTConfigurationException('Failed to generate master key for {}: {}'.format(corefiler, e))
                retries -= 1
                self._sleep()

            log.info("Activating master key for {}".format(corefiler))
            retries = self.service.XMLRPC_RETRIES
            while True:
                try:
                    r = self.xmlrpc().corefiler.activateMasterKey(corefiler, key['keyId'], key['recoveryFile'])
                    if r != 'success':
                        raise vFXTConfigurationException(response)
                    break
                except Exception as e:
                    log.debug(e)
                    if retries == 0:
                        raise vFXTConfigurationException('Failed to activate master key for {}: {}'.format(corefiler, e))
                    retries -= 1
                    self._sleep()

        log.info("Waiting for corefiler exports to show up")
        retries = self.service.WAIT_FOR_SUCCESS
        while True:
            try:
                exports = self.xmlrpc().corefiler.listExports(corefiler)
                if '/' in [export['path'] for export in exports[corefiler]]:
                    break
            except Exception as e:
                log.debug(e)
            if retries == 0:
                # try and remove it
                if options.get('remove_on_fail'):
                    try:
                        self.remove_corefiler(corefiler)
                    except Exception as e:
                        log.error("Failed to remove corefiler {}: {}".format(corefiler,e))
                raise vFXTConfigurationException("Timed out waiting for {} exports".format(corefiler))
            if retries % 10 == 0:
                self._log_conditions(self.xmlrpc())
            retries -= 1
            self._sleep()

        log.info("*** IT IS STRONGLY RECOMMENDED THAT YOU CREATE A NEW CLOUD ENCRYPTION KEY AND SAVE THE")
        log.info("*** KEY FILE (AND PASSWORD) BEFORE USING YOUR NEW CLUSTER.  WITHOUT THESE, IT WILL NOT")
        log.info("*** BE POSSIBLE TO RECOVER YOUR DATA AFTER A FAILURE")
        log.info("Do this at https://{}/avere/fxt/cloudFilerKeySettings.php".format(self.mgmt_ip))

        return key

    def attach_corefiler(self, corefiler, networkname, **options):
        '''Attach a Corefiler

            Arguments:
                corefiler (str): name of the corefiler to create
                networkname (str): network reachable name/address of the filer
                wait_for_export (str, optional): an export to watch for (defaults any exports)

            Raises: vFXTConfigurationException
        '''
        if corefiler in self.xmlrpc().corefiler.list():
            raise vFXTConfigurationException("Corefiler {} exists".format(corefiler))

        try:
            socket.gethostbyname(networkname)
        except Exception as e:
            raise vFXTConfigurationException("Unknown host {}: {}".format(corefiler, e))

        log.info("Creating corefiler {}".format(corefiler))
        activity = self.xmlrpc().corefiler.create(corefiler, networkname)
        if activity != 'success':
            while True:
                response = self.xmlrpc().cluster.getActivity(activity)
                if 'state' in response and response['state'] == 'success':
                    break
                if 'status' in response and response['status'] == 'failure':
                    raise vFXTConfigurationException('Failed to create corefiler {}'.format(corefiler))
                self._sleep()
        if corefiler not in self.xmlrpc().corefiler.list():
            raise vFXTConfigurationException('Failed to create corefiler {}'.format(corefiler))

        log.info("Waiting for corefiler exports to show up")
        retries = self.service.WAIT_FOR_NFS_EXPORTS
        wait_for_export = options.get('wait_for_export', None)
        while True:
            try:
                exports = self.xmlrpc().corefiler.listExports(corefiler)
                if wait_for_export:
                    if wait_for_export in [export['path'] for export in exports[corefiler]]:
                        break
                else:
                    if len(exports[corefiler]) > 0:
                        break
            except Exception as e:
                log.debug(e)
            if retries % 10 == 0:
                self._log_conditions(self.xmlrpc())
            if retries == 0:
                # try and remove it
                try:
                    self.remove_corefiler(corefiler)
                except Exception as e:
                    log.error("Failed to remove corefiler {}: {}".format(corefiler,e))
                raise vFXTConfigurationException("Timed out waiting for {} exports".format(corefiler))

            retries -= 1
            self._sleep()

    def remove_corefiler(self, corefiler):
        '''Remove a corefiler

            Arguments:
                corefiler (str): the name of the corefiler

            Raises vFXTConfigurationException
        '''
        try:
            xmlrpc = self.xmlrpc()

            response = xmlrpc.system.enableAPI('maintenance')
            if response != 'success':
                raise vFXTConfigurationException("Failed to enable maintenance API")

            activity =  xmlrpc.corefiler.remove(corefiler)
            if activity != 'success':
                while True:
                    response = xmlrpc.cluster.getActivity(activity)
                    if 'state' in response and response['state'] == 'success':
                        break
                    if 'status' in response and response['status'] == 'failure':
                        raise vFXTConfigurationException("Failed to remove corefiler {}: {}".format(corefiler, response))
                    self._sleep()
        except vFXTConfigurationException as e:
            log.debug(e)
            raise
        except Exception as e:
            raise vFXTConfigurationException(e)

    def add_vserver(self, name, size=0, netmask=None, start_address=None, end_address=None, retries=ServiceBase.WAIT_FOR_OPERATION):
        '''Add a Vserver

            Arguments:
                name (str): name of the vserver
                size (int, optional): size of the vserver address range (defaults to cluster size)
                netmask (str, optional): Network mask for the vserver range
                start_address (str, optional): Starting network address for the vserver range
                end_address (str, optional): Ending network address for the vserver range
                retries (int, optional): number of retries

            Calling with netmask, start_address, and end_address will define the vserver with
            those values.

            Otherwise, calling with or without a size leads to the addresses being determined via
            get_available_addresses().
        '''
        if not all([netmask, start_address, end_address]):
            in_use_addrs        = self.in_use_addresses()
            vserver_ips,netmask = self.service.get_available_addresses(count=size or len(self.nodes), contiguous=True, in_use=in_use_addrs)
            start_address       = vserver_ips[0]
            end_address         = vserver_ips[-1]
        else:
            # Validate
            vserver_ips = Cidr.expand_address_range(start_address, end_address)
            if len(vserver_ips) < len(self.nodes):
                log.warn("Adding vserver address range without enough addresses for all nodes")

        log.info("Creating vserver {} ({}-{}/{})".format(name, start_address, end_address, netmask))
        activity = self.xmlrpc().vserver.create(name, {'firstIP': start_address, 'lastIP': end_address, 'netmask':netmask})
        if activity != 'success':
            while True:
                r = self.xmlrpc().cluster.getActivity(activity)
                if 'state' in r and r['state'] == 'success':
                    break
                if 'status' in r and r['status'] == 'failure':
                    raise vFXTConfigurationException("Failed to create vserver {}".format(name))
                if retries == 0:
                    raise vFXTConfigurationException("Timed out waiting for vserver {}".format(name))
                retries -= 1
                self._sleep()

    def add_vserver_junction(self, vserver, corefiler, path=None, export='/', subdir=None, retries=ServiceBase.WAIT_FOR_STATUS):
        '''Add a Junction to a Vserver

            Arguments:
                vserver (str): name of the vserver
                corefiler (str): name of the corefiler
                path (str, optional): path of the junction (default /{corefiler})
                export (str, optional): export path (default /)
                subdir (str, optional): subdirectory within the export
                retries (int, optional): number of retries

            Raises: vFXTConfigurationException
        '''
        if not path:
            path = '/{}'.format(corefiler)
        if not path.startswith('/'):
            #raise vFXTConfigurationException("Junction path must start with /: {}".format(path))
            path = '/{}'.format(path)

        advanced = {}
        if subdir:
            advanced['subdir'] = subdir

        log.info("Creating junction to {} for vserver {}".format(corefiler, vserver))
        while True:
            try:
                response = self.xmlrpc().vserver.addJunction(vserver, path, corefiler, export, advanced)
                if response != 'success':
                    raise vFXTConfigurationException(response)
                break
            except Exception as e:
                if retries == 0:
                    raise vFXTConfigurationException("Failed to add junction to {}: {}".format(vserver, e))
                retries -= 1
                self._sleep()

        log.debug("Junctioned vserver {} with corefiler {} (path {}, export {})".format(vserver, corefiler, path, export))

    def wait_for_nodes_to_join(self, retries=ServiceBase.WAIT_FOR_HEALTH_CHECKS):
        '''This performs a check that the cluster configuration matches the
            nodes in the object, otherwise it will wait

            Arguments:
                retries (int): number of retries (default 600)

            Raises: vFXTConfigurationException
        '''
        expected = len(self.nodes)
        if expected > len(self.xmlrpc().node.list()):
            log.info("Waiting for all nodes to join")

            start_time = int(time.time())
            node_addresses = [n.ip() for n in self.nodes]
            while True:
                try:
                    found = len(self.xmlrpc().node.list())
                except Exception as e:
                    log.debug("Error getting node list: {}".format(e))
                    found = 1 # have to find one node at least

                if expected == found:
                    log.debug("Found {}".format(found))
                    break

                try:
                    # if nodes are upgrading, delay the retries..  unjoined node status include:
                    # 'joining: started'
                    # 'joining: almost done'
                    # 'joining: upgrade the image'
                    # 'joining: switch to the new image'
                    unjoined_status = [_['status'] for _ in self.xmlrpc().node.listUnconfiguredNodes() if _['address'] in node_addresses]
                    if any(['image' in _ for _ in unjoined_status]):
                        log.debug("Waiting for image upgrade to finish: {}".format(unjoined_status))
                        start_time = int(time.time())
                        continue
                except Exception as e:
                    log.debug("Failed to check unconfigured node status: {}".format(e))

                # for connectivity problems... we end up waiting a long time for
                # timeouts on the xmlrpc connection... so if we are taking too long
                # we should bail
                duration = int(time.time()) - start_time
                taking_too_long = duration > int(retries * 1.5)

                if retries == 0 or taking_too_long:
                    diff = expected - found
                    raise vFXTConfigurationException("Timed out waiting for {} node(s) to join.".format(diff))
                retries -= 1
                if retries % 10 == 0:
                    log.debug("Found {}, expected {}".format(found, expected))
                    self._log_conditions(self.xmlrpc())
                self._sleep()
        log.info("All nodes have joined the cluster.")

    def enable_ha(self, retries=ServiceBase.XMLRPC_RETRIES):
        '''Enable HA on the cluster

            Arguments:
                retries (int, optional): number of retries

            Raises: vFXTConfigurationException
        '''
        try:
            if self.xmlrpc().cluster.get()['ha'] == 'enabled':
                return
        except Exception as e:
            log.debug("Failed to check HA status: {}".format(e))

        log.info("Enabling HA mode")
        while True:
            try:
                status = self.xmlrpc().cluster.enableHA()
                if status != 'success':
                    raise vFXTConfigurationException(status)
                break
            except Exception as ha_e:
                log.debug(ha_e)
                if retries == 0:
                    raise vFXTConfigurationException("Failed to enable HA: {}".format(ha_e))
                retries -= 1
                self._sleep()

        # XXX settle time
        self._sleep(10)

    def rebalance_directory_managers(self, retries=ServiceBase.XMLRPC_RETRIES):
        '''Call rebalanceDirManagers via XMLRPC

            Arguments:
                retries (int): number of retries

            Raises: vFXTConfigurationException
        '''
        xmlrpc = self.xmlrpc()

        log.debug("Enabling maintenance API")
        response = xmlrpc.system.enableAPI('maintenance')
        if response != 'success':
            raise vFXTConfigurationException("Failed to enable maintenance API")

        log.info("Rebalancing directory managers")
        while retries > 0:
            try:
                status = xmlrpc.maint.rebalanceDirManagers()
                log.debug("rebalanceDirManagers returned {}".format(status))
                if status != 'success':
                    raise vFXTConfigurationException(response)
                return
            except Exception as e:
                try:
                    if int(e.faultCode) == 103: # #pylint: disable=no-member
                        return
                    if e.faultString.find('A directory manager rebalance operation is already scheduled') > -1: # #pylint: disable=no-member
                        return
                except: pass
                log.debug("Rebalance failed: {}, retrying...".format(e))
            retries -= 1
            self._sleep()

        raise vFXTStatusFailure("Waiting for cluster rebalance failed")

    def first_node_configuration(self, wait_for_state='yellow'):
        '''Basic configuration for the first cluster node

            Arguments:
                wait_for_state (str, optional): red, yellow, green cluster state
        '''
        if not self.mgmt_ip:
            raise vFXTConfigurationException("Cannot configure a cluster without a management address")
        log.info("Waiting for remote API connectivity to {}".format(self.mgmt_ip))
        xmlrpc = self.xmlrpc(retries=60) #pylint: disable=unused-variable

        self.set_default_proxy()

        # set support customerId to the cluster name
        log.info("Setting support customerId to {}".format(self.name))
        retries = ServiceBase.XMLRPC_RETRIES
        while True:
            try:
                response = self.xmlrpc().support.modify({'customerId':self.name})
                if response[0] != 'success':
                    raise vFXTConfigurationException(response)
                break
            except Exception as e:
                log.debug(e)
                if retries == 0:
                    log.error("Failed setting customerId: {}".format(e))
                    break
                retries -= 1

        # enable SPS for billing mode
        log.info("Enabling SPS")
        retries = ServiceBase.XMLRPC_RETRIES
        while True:
            try:
                support_opts = {'SPSLinkEnabled':'yes', 'statsMonitor': 'yes', 'generalInfo': 'yes'}
                if self.trace_level:
                    support_opts['traceLevel'] = self.trace_level
                    support_opts['rollingTrace'] = 'yes'
                response = self.xmlrpc().support.modify(support_opts)
                if response[0] != 'success':
                    raise vFXTConfigurationException(response)
                break
            except Exception as e:
                log.debug(e)
                if retries == 0:
                    log.error("Failed enabling SPS: {}".format(e))
                    break
                retries -= 1

        # try and enable HA early if we have support in the AvereOS release for single node
        try:
            self.enable_ha()
        except Exception as e:
            log.debug("Failed to enable early HA, will retry later: {}".format(e))
        self.verify_license()
        self.allow_node_join()
        self.wait_for_healthcheck(state=wait_for_state)

    def set_default_proxy(self, name=None):
        '''Set the default cluster proxy configuration

            Arguments:
                name (str, optional): proxy name (defaults to proxy hostname)
        '''
        if not self.proxy:
            log.debug("Skipping proxy configuration")
            return
        name   = name or self.proxy.hostname
        if not name or not self.proxy.geturl():
            raise vFXTConfigurationException("Unable to create proxy configuration: Bad proxy host")

        body = {'url': self.proxy.geturl(), 'user': self.proxy.username or '', 'password': self.proxy.password or ''}
        if name not in self.xmlrpc().cluster.listProxyConfigs():
            log.info("Setting proxy configuration")
            retries = self.service.XMLRPC_RETRIES
            while True:
                try:
                    response = self.xmlrpc().cluster.createProxyConfig(name, body)
                    if response != 'success':
                        raise vFXTConfigurationException(response)
                    break
                except Exception as e:
                    log.debug(e)
                    if retries == 0:
                        raise vFXTConfigurationException("Unable to create proxy configuration: {}".format(e))
                    retries -= 1
                    self._sleep()

        retries = self.service.XMLRPC_RETRIES
        while True:
            try:
                response = self.xmlrpc().cluster.modify({'proxy':name})
                if response != 'success':
                    raise vFXTConfigurationException(response)
                break
            except Exception as e:
                log.debug(e)
                if retries == 0:
                    raise vFXTConfigurationException("Unable to configure cluster proxy configuration: {}".format(e))
                retries -= 1
                self._sleep()

    def allow_node_join(self, enable=True):
        '''Enable node join configuration

            Arguments:
                enable (bool, optional): Allow nodes to join
        '''
        log.info("Setting node join policy")
        retries = self.service.XMLRPC_RETRIES
        setting = 'yes' if enable else 'no'
        while True:
            try:
                response = self.xmlrpc().cluster.modify({'allowAllNodesToJoin':setting})
                if response != 'success':
                    raise vFXTConfigurationException("Failed to update allow node join configuration: {}".format(response))
                break
            except Exception as e:
                log.debug(e)
                if retries == 0:
                    raise
                retries -= 1
                self._sleep()

    def refresh(self):
        '''Refresh instance data of cluster nodes from the backend service'''
        for n in self.nodes:
            n.refresh()

    def reload(self):
        '''Reload all cluster information'''
        if self.is_on(): # reread configuration, uses xmlrpc so must be on
            self.load_cluster_information()
        else:
            self.refresh()

    def export(self):
        '''Export the cluster object in an easy to serialize format'''
        return {
            'name':self.name,
            'mgmt_ip':self.mgmt_ip,
            'admin_password':self.admin_password,
            'nodes':[n.instance_id for n in self.nodes]
        }

    def _sleep(self, duration=None):
        '''General sleep handling'''
        time.sleep(duration or self.service.POLLTIME)

    @classmethod
    def valid_cluster_name(cls, name):
        '''Validate the cluster name

            Returns: bool
        '''
        name_len = len(name)
        if name_len < 1 or name_len > 128:
            return False
        if re.search('^[a-z]([-a-z0-9]*[a-z0-9])?$', name):
            return True
        return False

    def in_use_addresses(self, category='all'):
        '''Get in use addresses from the cluster

            Arguments:
                category (str): all (default), mgmt, vserver, cluster
        '''
        addresses = set()

        if category in ['all','mgmt']:
            addresses.update([self.xmlrpc().cluster.get()['mgmtIP']['IP']])

        if category in ['all','vserver']:
            for vs in self.xmlrpc().vserver.list():
                data = self.xmlrpc().vserver.get(vs)
                for client_range in data[vs]['clientFacingIPs']:
                    first = client_range['firstIP']
                    last  = client_range['lastIP']
                    range_addrs = Cidr.expand_address_range(first, last)
                    addresses.update(range_addrs)

        if category in ['all','cluster']:
            data = self.xmlrpc().cluster.get()
            for cluster_range in data['clusterIPs']:
                first = cluster_range['firstIP']
                last  = cluster_range['lastIP']
                range_addrs = Cidr.expand_address_range(first, last)
                addresses.update(range_addrs)

        return list(addresses)

    def set_node_naming_policy(self):
        '''Rename nodes internally and set the default node prefix

            This sets the node names internally to match the service instance
            names.  This also sets the node prefix to be the cluster name.
        '''
        if not self.nodes:
            log.debug("No nodes to rename, skipping")
            return

        node_ip_map = {_.ip():_.name() for _ in self.nodes}

        # rename nodes with cluster prefix
        log.info("Setting node naming policy")

        # first pass, rename new mismatched nodes to their node id
        retries = ServiceBase.XMLRPC_RETRIES
        while True:
            try:
                xmlrpc = self.xmlrpc()
                node_names = xmlrpc.node.list()
                nodes = [xmlrpc.node.get(_).values()[0] for _ in node_names]
                for node in nodes:
                    node_name = node_ip_map.get(node['primaryClusterIP']['IP'], None)
                    if node_name and node_name != node['name'] and node_name in node_names:
                        log.debug("Renaming new node {} -> {}".format(node['name'], node['id']))
                        xmlrpc.node.rename(node['name'], node['id'])
                break
            except Exception as e:
                log.debug(e)
                if retries == 0:
                    log.error("Failed to rename nodes: {}".format(e))
                    break
                retries -= 1

        # second pass, rename all nodes to their instance names
        retries = ServiceBase.XMLRPC_RETRIES
        while True:
            try:
                xmlrpc = self.xmlrpc()
                node_names = xmlrpc.node.list()
                nodes = [xmlrpc.node.get(_).values()[0] for _ in node_names]
                for node in nodes:
                    node_name = node_ip_map.get(node['primaryClusterIP']['IP'], None)
                    if node_name and node_name != node['name'] and node_name not in node_names:
                        log.debug("Renaming node {} -> {}".format(node['name'], node_name))
                        xmlrpc.node.rename(node['name'], node_name)
                break
            except Exception as e:
                log.debug(e)
                if retries == 0:
                    log.error("Failed to rename nodes: {}".format(e))
                    break
                retries -= 1


