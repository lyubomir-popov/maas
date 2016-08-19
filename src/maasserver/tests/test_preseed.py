# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test `maasserver.preseed` and related bits and bobs."""

__all__ = []

import http.client
import os
from pipes import quote
from unittest.mock import sentinel
from urllib.parse import urlparse

from django.conf import settings
from django.core.urlresolvers import reverse
from maasserver import preseed as preseed_module
from maasserver.clusterrpc.testing.boot_images import make_rpc_boot_image
from maasserver.enum import (
    FILESYSTEM_TYPE,
    NODE_STATUS,
    PRESEED_TYPE,
)
from maasserver.exceptions import (
    ClusterUnavailable,
    MissingBootImage,
)
from maasserver.models import (
    BootResource,
    Config,
    PackageRepository,
)
from maasserver.preseed import (
    compose_curtin_archive_config,
    compose_curtin_kernel_preseed,
    compose_curtin_maas_reporter,
    compose_curtin_swap_preseed,
    compose_curtin_verbose_preseed,
    compose_enlistment_preseed_url,
    compose_preseed_url,
    curtin_maas_reporter,
    GENERIC_FILENAME,
    get_archive_config,
    get_curtin_config,
    get_curtin_context,
    get_curtin_image,
    get_curtin_installer_url,
    get_curtin_merged_config,
    get_curtin_userdata,
    get_enlist_preseed,
    get_netloc_and_path,
    get_node_preseed_context,
    get_preseed,
    get_preseed_context,
    get_preseed_filenames,
    get_preseed_template,
    get_preseed_type_for,
    load_preseed_template,
    make_clean_repo_name,
    PreseedTemplate,
    render_enlistment_preseed,
    render_preseed,
    split_subarch,
    TemplateNotFoundError,
)
from maasserver.rpc.testing.mixins import PreseedRPCMixin
from maasserver.testing.architecture import make_usable_architecture
from maasserver.testing.config import RegionConfigurationFixture
from maasserver.testing.factory import factory
from maasserver.testing.osystems import make_usable_osystem
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.utils import absolute_reverse
from maasserver.utils.curtin import curtin_supports_webhook_events
from maastesting.matchers import (
    MockCalledOnceWith,
    MockNotCalled,
)
from metadataserver.models import NodeKey
from provisioningserver.drivers.osystem.ubuntu import UbuntuOS
from provisioningserver.rpc.exceptions import NoConnectionsAvailable
from provisioningserver.utils.enum import map_enum
from testtools.matchers import (
    AllMatch,
    Contains,
    ContainsAll,
    HasLength,
    IsInstance,
    MatchesAll,
    Not,
    StartsWith,
)
import yaml


class BootImageHelperMixin:

    def make_rpc_boot_image_for(self, node, purpose):
        osystem = node.get_osystem()
        series = node.get_distro_series()
        arch, subarch = node.split_arch()
        return make_rpc_boot_image(
            osystem=osystem, release=series,
            architecture=arch, subarchitecture=subarch,
            purpose=purpose)

    def configure_get_boot_images_for_node(self, node, purpose):
        boot_image = self.make_rpc_boot_image_for(node, purpose)
        self.patch(
            preseed_module,
            'get_boot_images_for').return_value = [boot_image]


class TestSplitSubArch(MAASServerTestCase):
    """Tests for `split_subarch`."""

    def test_split_subarch_returns_list(self):
        self.assertEqual(['amd64'], split_subarch('amd64'))

    def test_split_subarch_splits_sub_architecture(self):
        self.assertEqual(['amd64', 'test'], split_subarch('amd64/test'))


class TestGetNetlocAndPath(MAASServerTestCase):
    """Tests for `get_netloc_and_path`."""

    def test_get_netloc_and_path(self):
        input_and_results = [
            ('http://name.domain:66/my/path', ('name.domain:66', 'my/path')),
            ('http://name.domain:80/my/path', ('name.domain:80', 'my/path')),
            ('http://name.domain/my/path', ('name.domain', 'my/path')),
            ('https://domain/path', ('domain', 'path')),
            ('http://domain:12', ('domain:12', '')),
            ('http://domain/', ('domain', '')),
            ('http://domain', ('domain', '')),
            ]
        inputs = [input for input, _ in input_and_results]
        results = [result for _, result in input_and_results]
        self.assertEqual(results, list(map(get_netloc_and_path, inputs)))


class TestGetPreseedFilenames(MAASServerTestCase):
    """Tests for `get_preseed_filenames`."""

    def test__returns_filenames(self):
        hostname = factory.make_string()
        prefix = factory.make_string()
        osystem = factory.make_string()
        release = factory.make_string()
        node = factory.make_Node(hostname=hostname)
        arch, subarch = node.architecture.split('/')
        self.assertSequenceEqual(
            [
                '%s_%s_%s_%s_%s_%s' % (
                    prefix, osystem, arch, subarch, release, hostname),
                '%s_%s_%s_%s_%s' % (prefix, osystem, arch, subarch, release),
                '%s_%s_%s_%s' % (prefix, osystem, arch, subarch),
                '%s_%s_%s' % (prefix, osystem, arch),
                '%s_%s' % (prefix, osystem),
                '%s' % prefix,
                'generic',
            ],
            list(get_preseed_filenames(
                node, prefix, osystem, release, default=True)))

    def test__returns_limited_filenames_if_node_is_None(self):
        osystem = factory.make_string()
        release = factory.make_string()
        prefix = factory.make_string()
        self.assertSequenceEqual(
            [
                '%s_%s_%s' % (prefix, osystem, release),
                '%s_%s' % (prefix, osystem),
                '%s' % prefix,
            ],
            list(get_preseed_filenames(None, prefix, osystem, release)))

    def test__supports_empty_prefix(self):
        hostname = factory.make_string()
        osystem = factory.make_string()
        release = factory.make_string()
        node = factory.make_Node(hostname=hostname)
        arch, subarch = node.architecture.split('/')
        self.assertSequenceEqual(
            [
                '%s_%s_%s_%s_%s' % (osystem, arch, subarch, release, hostname),
                '%s_%s_%s_%s' % (osystem, arch, subarch, release),
                '%s_%s_%s' % (osystem, arch, subarch),
                '%s_%s' % (osystem, arch),
                '%s' % osystem,
            ],
            list(get_preseed_filenames(node, '', osystem, release)))

    def test__returns_list_without_default(self):
        # If default=False is passed to get_preseed_filenames, the
        # returned list won't include the default template name as a
        # last resort template.
        hostname = factory.make_string()
        prefix = factory.make_string()
        release = factory.make_string()
        node = factory.make_Node(hostname=hostname)
        self.assertSequenceEqual(
            'generic',
            list(get_preseed_filenames(
                node, prefix, release, default=True))[-1])

    def test__returns_list_with_default(self):
        # If default=True is passed to get_preseed_filenames, the
        # returned list will include the default template name as a
        # last resort template.
        hostname = factory.make_string()
        prefix = factory.make_string()
        release = factory.make_string()
        node = factory.make_Node(hostname=hostname)
        self.assertSequenceEqual(
            prefix,
            list(get_preseed_filenames(
                node, prefix, release, default=False))[-1])

    def test__returns_backward_compatible_name_for_ubuntu_without_prefix(self):
        # If the OS is Ubuntu, also include backward-compatible filenames.
        # See bug 1439366 for details.
        hostname = factory.make_string()
        osystem = UbuntuOS().name
        release = factory.make_string()
        node = factory.make_Node(hostname=hostname)
        arch, subarch = node.architecture.split('/')
        self.assertSequenceEqual(
            [
                '%s_%s_%s_%s_%s' % (osystem, arch, subarch, release, hostname),
                '%s_%s_%s_%s' % (arch, subarch, release, hostname),
                '%s_%s_%s_%s' % (osystem, arch, subarch, release),
                '%s_%s_%s' % (arch, subarch, release),
                '%s_%s_%s' % (osystem, arch, subarch),
                '%s_%s' % (arch, subarch),
                '%s_%s' % (osystem, arch),
                '%s' % (arch),
                '%s' % osystem,
            ],
            list(get_preseed_filenames(node, '', osystem, release)))

    def test__returns_backward_compatible_name_for_ubuntu_with_prefix(self):
        # If the OS is Ubuntu, also include backward-compatible filenames.
        # See bug 1439366 for details.
        hostname = factory.make_string()
        osystem = UbuntuOS().name
        release = factory.make_string()
        node = factory.make_Node(hostname=hostname)
        arch, subarch = node.architecture.split('/')
        prefix = factory.make_string()
        self.assertSequenceEqual(
            [
                '%s_%s_%s_%s_%s_%s' % (
                    prefix, osystem, arch, subarch, release, hostname),
                '%s_%s_%s_%s_%s' % (prefix, arch, subarch, release, hostname),
                '%s_%s_%s_%s_%s' % (prefix, osystem, arch, subarch, release),
                '%s_%s_%s_%s' % (prefix, arch, subarch, release),
                '%s_%s_%s_%s' % (prefix, osystem, arch, subarch),
                '%s_%s_%s' % (prefix, arch, subarch),
                '%s_%s_%s' % (prefix, osystem, arch),
                '%s_%s' % (prefix, arch),
                '%s_%s' % (prefix, osystem),
                '%s' % prefix,
            ],
            list(get_preseed_filenames(node, prefix, osystem, release)))


class TestConfiguration(MAASServerTestCase):
    """Test for correct configuration of the preseed component."""

    def test_setting_defined(self):
        self.assertThat(
            settings.PRESEED_TEMPLATE_LOCATIONS,
            AllMatch(IsInstance(str)))


class TestGetPreseedTemplate(MAASServerTestCase):
    """Tests for `get_preseed_template`."""

    def test_get_preseed_template_returns_None_if_no_template_locations(self):
        # get_preseed_template() returns None when no template locations are
        # defined.
        self.patch(settings, "PRESEED_TEMPLATE_LOCATIONS", [])
        self.assertEqual(
            (None, None),
            get_preseed_template(
                (factory.make_string(), factory.make_string())))

    def test_get_preseed_template_returns_None_when_no_filenames(self):
        # get_preseed_template() returns None when no filenames are passed in.
        self.patch(settings, "PRESEED_TEMPLATE_LOCATIONS", [self.make_dir()])
        self.assertEqual((None, None), get_preseed_template(()))

    def test_get_preseed_template_find_template_in_first_location(self):
        template_content = factory.make_string()
        template_path = self.make_file(contents=template_content)
        template_filename = os.path.basename(template_path)
        locations = [
            os.path.dirname(template_path),
            self.make_dir(),
            ]
        self.patch(settings, "PRESEED_TEMPLATE_LOCATIONS", locations)
        self.assertEqual(
            (template_path, template_content),
            get_preseed_template([template_filename]))

    def test_get_preseed_template_find_template_in_last_location(self):
        template_content = factory.make_string()
        template_path = self.make_file(contents=template_content)
        template_filename = os.path.basename(template_path)
        locations = [
            self.make_dir(),
            os.path.dirname(template_path),
            ]
        self.patch(settings, "PRESEED_TEMPLATE_LOCATIONS", locations)
        self.assertEqual(
            (template_path, template_content),
            get_preseed_template([template_filename]))


class TestLoadPreseedTemplate(MAASServerTestCase):
    """Tests for `load_preseed_template`."""

    def setUp(self):
        super(TestLoadPreseedTemplate, self).setUp()
        self.location = self.make_dir()
        self.patch(
            settings, "PRESEED_TEMPLATE_LOCATIONS", [self.location])

    def create_template(self, location, name, content=None):
        # Create a tempita template in the given `self.location` with the
        # given `name`.  If content is not provided, a random content
        # will be put inside the template.
        path = os.path.join(self.location, name)
        rendered_content = None
        if content is None:
            rendered_content = factory.make_string()
            content = '{{def stuff}}%s{{enddef}}{{stuff}}' % rendered_content
        with open(path, "wb") as outf:
            outf.write(content.encode("utf-8"))
        return rendered_content

    def test_load_preseed_template_returns_PreseedTemplate(self):
        name = factory.make_string()
        self.create_template(self.location, name)
        node = factory.make_Node()
        template = load_preseed_template(node, name)
        self.assertIsInstance(template, PreseedTemplate)

    def test_load_preseed_template_raises_if_no_template(self):
        node = factory.make_Node()
        unknown_template_name = factory.make_string()
        self.assertRaises(
            TemplateNotFoundError, load_preseed_template, node,
            unknown_template_name)

    def test_load_preseed_template_generic_lookup(self):
        # The template lookup method ends up picking up a template named
        # 'generic' if no more specific template exist.
        content = self.create_template(self.location, GENERIC_FILENAME)
        node = factory.make_Node(hostname=factory.make_string())
        template = load_preseed_template(node, factory.make_string())
        self.assertEqual(content, template.substitute())

    def test_load_preseed_template_prefix_lookup(self):
        # 2nd last in the hierarchy is a template named 'prefix'.
        prefix = factory.make_string()
        # Create the generic template.  This one will be ignored due to the
        # presence of a more specific template.
        self.create_template(self.location, GENERIC_FILENAME)
        # Create the 'prefix' template.  This is the one which will be
        # picked up.
        content = self.create_template(self.location, prefix)
        node = factory.make_Node(hostname=factory.make_string())
        template = load_preseed_template(node, prefix)
        self.assertEqual(content, template.substitute())

    def test_load_preseed_template_node_specific_lookup(self):
        # At the top of the lookup hierarchy is a template specific to this
        # node.  It will be used first if it's present.
        prefix = factory.make_string()
        osystem = factory.make_string()
        release = factory.make_string()
        # Create the generic and 'prefix' templates.  They will be ignored
        # due to the presence of a more specific template.
        self.create_template(self.location, GENERIC_FILENAME)
        self.create_template(self.location, prefix)
        node = factory.make_Node(hostname=factory.make_string())
        node_template_name = "%s_%s_%s_%s_%s" % (
            prefix, osystem, node.architecture.replace('/', '_'),
            release, node.hostname)
        # Create the node-specific template.
        content = self.create_template(self.location, node_template_name)
        template = load_preseed_template(node, prefix, osystem, release)
        self.assertEqual(content, template.substitute())

    def test_load_preseed_template_with_inherits(self):
        # A preseed file can "inherit" from another file.
        prefix = factory.make_string()
        # Create preseed template.
        master_template_name = factory.make_string()
        preseed_content = '{{inherit "%s"}}' % master_template_name
        self.create_template(self.location, prefix, preseed_content)
        master_content = self.create_template(
            self.location, master_template_name)
        node = factory.make_Node()
        template = load_preseed_template(node, prefix)
        self.assertEqual(master_content, template.substitute())

    def test_load_preseed_template_parent_lookup_doesnt_include_default(self):
        # The lookup for parent templates does not include the default
        # 'generic' file.
        prefix = factory.make_string()
        # Create 'generic' template.  It won't be used because the
        # lookup for parent templates does not use the 'generic' template.
        self.create_template(self.location, GENERIC_FILENAME)
        unknown_master_template_name = factory.make_string()
        # Create preseed template.
        preseed_content = '{{inherit "%s"}}' % unknown_master_template_name
        self.create_template(self.location, prefix, preseed_content)
        node = factory.make_Node()
        template = load_preseed_template(node, prefix)
        self.assertRaises(
            TemplateNotFoundError, template.substitute)


class TestPreseedContext(MAASServerTestCase):
    """Tests for `get_preseed_context`."""

    def add_main_archive(self, url, arches=PackageRepository.MAIN_ARCHES):
        PackageRepository.objects.create(
            name=factory.make_name(),
            description=factory.make_string(),
            url=url,
            arches=arches,
            default=True)

    def add_ports_archive(self, url, arches=PackageRepository.PORTS_ARCHES):
        PackageRepository.objects.create(
            name=factory.make_name(),
            description=factory.make_string(),
            url=url,
            arches=arches,
            default=True)

    def test_get_preseed_context_contains_keys(self):
        context = get_preseed_context()
        self.assertItemsEqual(
            ['osystem', 'release', 'metadata_enlist_url', 'server_host',
             'server_url', 'main_archive_hostname', 'main_archive_directory',
             'ports_archive_hostname', 'ports_archive_directory',
             'enable_http_proxy', 'http_proxy', 'syslog_host_port'],
            context)

    def test_get_preseed_context_archive_refs(self):
        PackageRepository.objects.all().delete()
        # urlparse lowercases the hostnames. That should not have any
        # impact but for testing, create lower-case hostnames.
        main_archive = factory.make_url(
            scheme="http", netloc="main-archive.example.com")
        ports_archive = factory.make_url(
            scheme="http", netloc="ports-archive.example.com")
        self.add_main_archive(main_archive)
        self.add_ports_archive(ports_archive)
        context = get_preseed_context()
        parsed_main_archive = urlparse(main_archive)
        parsed_ports_archive = urlparse(ports_archive)
        self.assertEqual(
            (
                parsed_main_archive.hostname,
                parsed_main_archive.path.lstrip('/'),
                parsed_ports_archive.hostname,
                parsed_ports_archive.path.lstrip('/'),
            ),
            (
                context['main_archive_hostname'],
                context['main_archive_directory'],
                context['ports_archive_hostname'],
                context['ports_archive_directory'],
            ))


class TestNodePreseedContext(
        PreseedRPCMixin, BootImageHelperMixin, MAASTransactionServerTestCase):
    """Tests for `get_node_preseed_context`."""

    def test_get_node_preseed_context_contains_keys(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller)
        self.configure_get_boot_images_for_node(node, 'install')
        release = factory.make_string()
        context = get_node_preseed_context(node, release)
        self.assertItemsEqual(
            ['driver', 'driver_package', 'node',
             'node_disable_pxe_data', 'node_disable_pxe_url',
             'preseed_data', 'third_party_drivers', 'license_key',
             ],
            context)

    def test_context_contains_third_party_drivers(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller)
        self.configure_get_boot_images_for_node(node, 'install')
        release = factory.make_string()
        enable_third_party_drivers = factory.pick_bool()
        Config.objects.set_config(
            'enable_third_party_drivers', enable_third_party_drivers)
        context = get_node_preseed_context(node, release)
        self.assertEqual(
            enable_third_party_drivers,
            context['third_party_drivers'])


class TestPreseedTemplate(MAASServerTestCase):
    """Tests for class:`PreseedTemplate`."""

    def test_escape_shell(self):
        template = PreseedTemplate("{{var|escape.shell}}")
        var = "$ ! ()"
        observed = template.substitute(var=var)
        self.assertEqual(quote(var), observed)


class TestRenderPreseed(
        PreseedRPCMixin, BootImageHelperMixin, MAASTransactionServerTestCase):
    """Tests for `render_preseed`.

    These tests check that the templates render (i.e. that no variable is
    missing).
    """

    # Create a scenario for each possible value of PRESEED_TYPE except
    # enlistment. Those have their own test case.
    scenarios = [
        (name, {'preseed': value})
        for name, value in map_enum(PRESEED_TYPE).items()
        if not value.startswith('enlist')
    ]

    def test_render_preseed(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller)
        self.configure_get_boot_images_for_node(node, 'install')
        preseed = render_preseed(node, self.preseed, "precise")
        # The test really is that the preseed is rendered without an
        # error.
        self.assertIsInstance(preseed, bytes)

    def test_get_preseed_uses_rack_controller_url(self):
        ng_url = 'http://%s' % factory.make_hostname()
        self.rpc_rack_controller.url = ng_url
        self.rpc_rack_controller.save()
        maas_url = factory.make_simple_http_url()
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller,
            status=NODE_STATUS.COMMISSIONING)
        self.configure_get_boot_images_for_node(node, 'install')
        self.useFixture(RegionConfigurationFixture(maas_url=maas_url))
        preseed = render_preseed(node, self.preseed, "precise")
        self.assertThat(
            preseed.decode("utf-8"),
            MatchesAll(
                Contains(ng_url),
                Not(Contains(maas_url)),
            ))


class TestRenderEnlistmentPreseed(MAASServerTestCase):
    """Tests for `render_enlistment_preseed`."""

    # Create a scenario for each possible value of PRESEED_TYPE for
    # enlistment. The rest have their own test case.
    scenarios = [
        (name, {'preseed': value})
        for name, value in map_enum(PRESEED_TYPE).items()
        if value.startswith('enlist')
    ]

    def test_render_enlistment_preseed(self):
        preseed = render_enlistment_preseed(self.preseed, "precise")
        # The test really is that the preseed is rendered without an
        # error.
        self.assertIsInstance(preseed, bytes)

    def test_render_enlistment_preseed_valid_yaml(self):
        preseed = render_enlistment_preseed(self.preseed, "precise")
        self.assertTrue(yaml.safe_load(preseed))

    def test_get_preseed_uses_rack_controller_maas_url(self):
        url = 'http://%s' % factory.make_hostname()
        maas_url = factory.make_simple_http_url()
        self.useFixture(RegionConfigurationFixture(maas_url=maas_url))
        rack_controller = factory.make_RackController(url=url)
        preseed = render_enlistment_preseed(
            self.preseed, "precise", rack_controller=rack_controller)
        self.assertThat(
            preseed.decode("utf-8"),
            MatchesAll(
                Contains(url),
                Not(Contains(maas_url)),
            ))


class TestComposeCurtinMAASReporter(MAASServerTestCase):

    def load_reporter(self, preseeds):
        [reporter_yaml] = preseeds
        return yaml.safe_load(reporter_yaml)

    def test__curtin_maas_reporter_with_events_support(self):
        node = factory.make_Node_with_Interface_on_Subnet()
        token = NodeKey.objects.get_token_for_node(node)
        reporter = curtin_maas_reporter(node, True)
        self.assertItemsEqual(
            ['reporting', 'install'], list(reporter.keys()))
        self.assertEqual(
            absolute_reverse(
                'metadata-status', args=[node.system_id],
                base_url=node.get_boot_primary_rack_controller().url),
            reporter['reporting']['maas']['endpoint'])
        self.assertEqual(
            'webhook',
            reporter['reporting']['maas']['type'])
        self.assertEqual(
            token.consumer.key,
            reporter['reporting']['maas']['consumer_key'])
        self.assertEqual(
            token.key,
            reporter['reporting']['maas']['token_key'])
        self.assertEqual(
            token.secret,
            reporter['reporting']['maas']['token_secret'])

    def test__curtin_maas_reporter_without_events_support(self):
        node = factory.make_Node_with_Interface_on_Subnet()
        token = NodeKey.objects.get_token_for_node(node)
        reporter = curtin_maas_reporter(node, False)
        self.assertEqual(['reporter'], list(reporter.keys()))
        self.assertEqual(
            absolute_reverse(
                'curtin-metadata-version', args=['latest'],
                query={'op': 'signal'},
                base_url=node.get_boot_primary_rack_controller().url),
            reporter['reporter']['maas']['url'])
        self.assertEqual(
            token.consumer.key,
            reporter['reporter']['maas']['consumer_key'])
        self.assertEqual(
            token.key,
            reporter['reporter']['maas']['token_key'])
        self.assertEqual(
            token.secret,
            reporter['reporter']['maas']['token_secret'])

    def test__returns_list_of_yaml_strings_matching_curtin(self):
        preseeds = compose_curtin_maas_reporter(
            factory.make_Node_with_Interface_on_Subnet())
        self.assertIsInstance(preseeds, list)
        self.assertThat(preseeds, HasLength(1))
        reporter = self.load_reporter(preseeds)
        self.assertIsInstance(reporter, dict)
        if curtin_supports_webhook_events():
            self.assertItemsEqual(
                ['reporting', 'install'], list(reporter.keys()))
        else:
            self.assertItemsEqual(
                ['reporter'], list(reporter.keys()))


class TestComposeCurtinSwapSpace(MAASServerTestCase):

    def test__returns_null_swap_size(self):
        node = factory.make_Node()
        self.assertEqual(node.swap_size, None)
        swap_preseed = compose_curtin_swap_preseed(node)
        self.assertEqual(swap_preseed, [])

    def test__returns_set_swap_size(self):
        node = factory.make_Node()
        node.swap_size = 10 * 1000 ** 3
        swap_preseed = compose_curtin_swap_preseed(node)
        self.assertEqual(swap_preseed, ['swap: {size: 10000000000B}\n'])

    def test__suppresses_swap_file_when_swap_on_block_device(self):
        node = factory.make_Node()
        block_device = factory.make_BlockDevice(node=node)
        factory.make_Filesystem(
            fstype=FILESYSTEM_TYPE.SWAP, block_device=block_device)
        node.swap_size = None
        swap_preseed = compose_curtin_swap_preseed(node)
        self.assertEqual(swap_preseed, ['swap: {size: 0B}\n'])

    def test__suppresses_swap_file_when_swap_on_partition(self):
        node = factory.make_Node()
        partition = factory.make_Partition(node=node)
        factory.make_Filesystem(
            fstype=FILESYSTEM_TYPE.SWAP, partition=partition)
        node.swap_size = None
        swap_preseed = compose_curtin_swap_preseed(node)
        self.assertEqual(swap_preseed, ['swap: {size: 0B}\n'])


class TestComposeCurtinKernel(MAASServerTestCase):

    def test__returns_null_kernel(self):
        node = factory.make_Node()
        self.assertEqual(node.hwe_kernel, None)
        kernel_preseed = compose_curtin_kernel_preseed(node)
        self.assertEqual(kernel_preseed, [])

    def test__returns_set_kernel(self):
        self.patch(
            BootResource.objects, 'get_kpackage_for_node').return_value = (
            'linux-image-generic-lts-vivid')
        node = factory.make_Node(hwe_kernel='hwe-v')
        self.assertEqual(node.hwe_kernel, 'hwe-v')
        kernel_preseed = compose_curtin_kernel_preseed(node)
        self.assertEqual(kernel_preseed,
                         ['kernel:\n' +
                          '  mapping: {}\n' +
                          '  package: linux-image-generic-lts-vivid\n'
                          ])


class TestComposeCurtinVerbose(MAASServerTestCase):

    def test__returns_empty_when_false(self):
        Config.objects.set_config("curtin_verbose", False)
        self.assertEqual([], compose_curtin_verbose_preseed())

    def test__returns_verbosity_config(self):
        Config.objects.set_config("curtin_verbose", True)
        preseed = compose_curtin_verbose_preseed()
        self.assertEqual({
            "verbosity": 3,
            "showtrace": True,
            }, yaml.load(preseed[0]))


class TestGetCurtinMergedConfig(MAASServerTestCase):

    def test__merges_configs_together(self):
        configs = [
            yaml.safe_dump({
                "maas": {
                    "test": "data"
                },
                "override": "data",
            }),
            yaml.safe_dump({
                "maas2": {
                    "test": "data2"
                },
                "override": "data2",
            }),
        ]
        mock_yaml_config = self.patch_autospec(
            preseed_module, "get_curtin_yaml_config")
        mock_yaml_config.return_value = configs
        self.assertEqual({
            "maas": {
                "test": "data"
            },
            "maas2": {
                "test": "data2"
            },
            "override": "data2",
        }, get_curtin_merged_config(sentinel.node))
        self.assertThat(mock_yaml_config, MockCalledOnceWith(sentinel.node))


class TestGetCurtinUserData(
        PreseedRPCMixin, BootImageHelperMixin, MAASTransactionServerTestCase):
    """Tests for `get_curtin_userdata`."""

    def test_get_curtin_userdata_calls_compose_curtin_config_on_ubuntu(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller)
        arch, subarch = node.architecture.split('/')
        main_url = PackageRepository.get_main_archive()
        factory.make_PackageRepository(
            url=main_url, default=True, arches=['i386', 'amd64'])
        self.configure_get_boot_images_for_node(node, 'xinstall')
        mock_compose_storage = self.patch(
            preseed_module, "compose_curtin_storage_config")
        mock_compose_network = self.patch(
            preseed_module, "compose_curtin_network_config")
        self.patch(
            preseed_module, "curtin_supports_custom_storage").value = True
        node.osystem = 'ubuntu'
        user_data = get_curtin_userdata(node)
        self.assertIn("PREFIX='curtin'", user_data)
        self.assertThat(mock_compose_storage, MockCalledOnceWith(node))
        self.assertThat(mock_compose_network, MockCalledOnceWith(node))

    def test_get_curtin_userdata_skips_storage_for_non_ubuntu(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller)
        arch, subarch = node.architecture.split('/')
        self.configure_get_boot_images_for_node(node, 'xinstall')
        mock_compose_storage = self.patch(
            preseed_module, "compose_curtin_storage_config")
        self.patch(
            preseed_module, "curtin_supports_custom_storage").value = True
        node.osystem = factory.make_name("osystem")
        user_data = get_curtin_userdata(node)
        self.assertIn("PREFIX='curtin'", user_data)
        self.assertThat(mock_compose_storage, MockNotCalled())

    def test_get_curtin_userdata_includes_networking_for_non_ubuntu(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller)
        arch, subarch = node.architecture.split('/')
        self.configure_get_boot_images_for_node(node, 'xinstall')
        mock_compose_network = self.patch(
            preseed_module, "compose_curtin_network_config")
        self.patch(
            preseed_module, "curtin_supports_custom_storage").value = True
        node.osystem = factory.make_name("osystem")
        user_data = get_curtin_userdata(node)
        self.assertIn("PREFIX='curtin'", user_data)
        self.assertThat(mock_compose_network, MockCalledOnceWith(node))

    def test_get_curtin_userdata_calls_curtin_supports_custom_storage(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller)
        arch, subarch = node.architecture.split('/')
        self.configure_get_boot_images_for_node(node, 'xinstall')
        mock_supports_storage = self.patch(
            preseed_module, "curtin_supports_custom_storage")
        mock_supports_storage.return_value = False

        user_data = get_curtin_userdata(node)
        self.assertIn("PREFIX='curtin'", user_data)
        self.assertThat(mock_supports_storage, MockCalledOnceWith())


class TestGetCurtinUserDataOS(
        PreseedRPCMixin, BootImageHelperMixin, MAASTransactionServerTestCase):
    """Tests for `get_curtin_userdata` using os specific scenarios."""

    # Create a scenario for each possible os specific preseed.
    scenarios = [
        (name, {'os_name': name})
        for name in ['centos', 'suse', 'windows']
    ]

    def test_get_curtin_userdata(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller, osystem=self.os_name)
        arch, subarch = node.architecture.split('/')
        self.configure_get_boot_images_for_node(node, 'xinstall')
        user_data = get_curtin_userdata(node)

        # Just check that the user data looks good.
        self.assertIn("PREFIX='curtin'", user_data)


class TestCurtinUtilities(
        PreseedRPCMixin, BootImageHelperMixin, MAASTransactionServerTestCase):
    """Tests for the curtin-related utilities."""

    def test_get_curtin_config(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller)
        self.configure_get_boot_images_for_node(node, 'xinstall')
        config = get_curtin_config(node)
        self.assertThat(
            config,
            ContainsAll(
                [
                    'mode: reboot',
                    "debconf_selections:",
                ]
            ))

    def make_fastpath_node(self, main_arch=None):
        """Return a `Node`, with FPI enabled, and the given main architecture.

        :param main_arch: A main architecture, such as `i386` or `armhf`.  A
            subarchitecture will be made up.
        """
        if main_arch is None:
            main_arch = factory.make_name('arch')
        arch = '%s/%s' % (main_arch, factory.make_name('subarch'))
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller, architecture=arch)
        return node

    def extract_archive_setting(self, userdata):
        """Extract the `uri` setting from `userdata` for both, the
        primary and security archives. This has replaced the
        `ubuntu_archive` setting"""
        userdata_lines = []
        for line in userdata.splitlines():
            line = line.strip()
            if line.startswith('uri'):
                userdata_lines.append(line)
        # We check for two. The uri for primary and security:
        # apt:
        #  preserve_sources_list: false
        #  primary:
        #  - arches: [default]
        #    uri: http://us.archive.ubuntu.com/ubuntu
        #  security:
        #  - arches: [default]
        #    uri: http://us.archive.ubuntu.com/ubuntu
        self.assertThat(userdata_lines, HasLength(2))
        key, value = userdata_lines[0].split(':', 1)
        return value.strip()

    def summarise_url(self, url):
        """Return just the hostname and path from `url`, normalised."""
        # This is needed because the userdata deliberately makes some minor
        # changes to the archive URLs, making it harder to recognise which
        # archive they use: slashes are added, schemes are hard-coded.
        parsed_result = urlparse(url)
        return parsed_result.netloc, parsed_result.path.strip('/')

    def test_compose_curtin_archive_config_uses_main_archive_for_i386(self):
        node = self.make_fastpath_node('i386')
        node.osystem = 'ubuntu'
        main_url = 'http://us.archive.ubuntu.com/ubuntu'
        factory.make_PackageRepository(
            url=main_url, default=True, arches=['i386', 'amd64'])
        self.configure_get_boot_images_for_node(node, 'xinstall')
        # compose_curtin_archive_config returns a list.
        userdata = compose_curtin_archive_config(node)
        archive = PackageRepository.objects.get_default_archive(
            node.split_arch()[0])
        self.assertEqual(
            archive.url,
            self.extract_archive_setting(userdata[0]))

    def test_compose_curtin_archive_config_uses_main_archive_for_amd64(self):
        node = self.make_fastpath_node('amd64')
        node.osystem = 'ubuntu'
        main_url = 'http://us.archive.ubuntu.com/ubuntu'
        factory.make_PackageRepository(
            url=main_url, default=True, arches=['i386', 'amd64'])
        self.configure_get_boot_images_for_node(node, 'xinstall')
        # compose_curtin_archive_config returns a list.
        userdata = compose_curtin_archive_config(node)
        archive = PackageRepository.objects.get_default_archive(
            node.split_arch()[0])
        self.assertEqual(
            archive.url,
            self.extract_archive_setting(userdata[0]))

    def test_compose_curtin_archive_config_uses_main_archive_key(self):
        node = self.make_fastpath_node('amd64')
        node.osystem = 'ubuntu'
        main_url = 'http://us.archive.ubuntu.com/ubuntu'
        key = """-----BEGIN PGP PUBLIC KEY BLOCK-----
Version: SKS 1.1.5
Comment: Hostname: keyserver.ubuntu.com

mQINBFXVlyMBEACqM3iz2EGJE0iE3/AAbNCnbBB25m3AWaSxJk+GJfkAAYWGqAKiuWceCcet
dNKNTKd8frSZFsRB7IceZr0u5sWpSYur6uoMNHzS8Y5cGdyAVrnEZtbdak652x13jlX7nrcE
9g//lD0w254XW1Loyy5YOGWfUmJkGImndFWtkqd1J7SCVMMW5l/nS4LwsOx/wTxL5m/cFQLi
67JyJGqszKXS88oHT1YFBWPyl1VcXifFwecH/32fRr6WGpEAaxGF4dO45WGvJIQs2yiT5f9h
a3tuJCbzI58t9BxiR1MMZ9AAPjdNO6JZkX2q+/uqgJg9IWNcJ4E+fCgl/hvoB3AURXHmaagH
7nMb/6OA/QFSbiR3eciSJ89cEkK+7d0br+p2+shO/dOV6lUrbidVVjiiTdmYlyXzuPcvPWVY
mXjDzsOi0sSZZNMq8G3/pAavjyGUvZtb781V1j9/8l3o5ScAPzzamT2W4rF+nCh1iHYz7+wP
2XDNifE/oK7fLNb0ig1G5S4PCqZHUp95LUaJrFczYCPwlERUxIC3B9a+UC3SdZmRuuSENWNs
YxKUlbU07GCrjxtcDhQHGQDVJDUGbqqkA4B/iKrwW3reA5fHo3yocQMX7YR6C2/Qn+wn/EoE
PIB1wkzAQvarnNCCdwjD5AB1VhANEFwUKMWHDEsofKOSTBYvgQARAQABtBZMYXVuY2hwYWQg
UFBBIGZvciBNQUFTiQI4BBMBAgAiBQJV1ZcjAhsDBgsJCAcDAgYVCAIJCgsEFgIDAQIeAQIX
gAAKCRAE5/3FaE1KHDH8D/9Mdc+4tw8foj6lILCgfBRi9S37tOyV2m5YvD+qRzefUYgFKXYx
leO+H9cjFH2XyHIBwa15dD/Yg+DkcAKb9f/a1llHNTzLkHiNVQl4tl8qeJPj2Obm53Hsjhaz
Igh0L208GRGJxO4HSBbrBTo8FNF00Cl52josZdG1mPCSDuJm1AkeY9q4WeAOnekquz2qjUa+
L8J8z+HVPC9rUryENXdwCyh3TE0G0occjUAsb5oOu3bcKSbVraq+trhjp9sz7o7O4lc4+cT2
gFIWl1Rp1djzXH8flU/s3U1vl0RcIFEZbuqsuDWukpxozq4M5y7VKq4y5dq7Y0PbMuJ0Dvgn
Bn4fbboMji4LYfgn++vosZv/MXkPIg6wubxdejVdrEoFRFxCcYqW4wObY8vxrvDrMjp4HrQ2
guN8OJDUYnLdVv9P1MMKDAMrDjRdy3NsBpd7GuA9hXRXBPZ8y74nIwCRjEDnIz5jsws9PxZI
VabieoCI6RibJMw8qpuicM97Ss2Uq5vURvTBQ3f6wYjCMsdtyqjz6TVJ3zwK9NPfMhXGVrrs
xBOxO382r6XXuUbTcXZTDjAkoMsBqfjidlGDGTb3Un0LkZJfpXrmZehyvO/GlsoYiFDhGf+E
XJzKwRUEuJlIkVEZ72OtuoUMoBrjuADRlJQUW0ZbcmpOxjK1c6w08nhSvA==
=QeWQ
-----END PGP PUBLIC KEY BLOCK-----"""
        factory.make_PackageRepository(
            url=main_url, default=True, arches=['i386', 'amd64'], key=key)
        self.configure_get_boot_images_for_node(node, 'xinstall')
        # compose_curtin_archive_config returns a list.
        userdata = get_archive_config(node)
        archive = PackageRepository.objects.get_default_archive(
            node.split_arch()[0])
        self.assertEqual(
            userdata['apt']['sources']['archive_key']['key'],
            archive.key)

    def test_compose_curtin_archive_config_has_ppa(self):
        node = self.make_fastpath_node('i386')
        node.osystem = 'ubuntu'
        node.distro_series = 'xenial'
        ppa_url = 'http://ppa.launchpad.net/maas/next/ubuntu'
        factory.make_PackageRepository(
            name='MAAS PPA', url=ppa_url, default=False,
            arches=['i386', 'amd64'])
        self.configure_get_boot_images_for_node(node, 'xinstall')
        # compose_curtin_archive_config returns a list.
        userdata = compose_curtin_archive_config(node)
        ppa = PackageRepository.objects.get_additional_repositories(
            node.split_arch()[0]).first()
        preseed = yaml.load(userdata[0])
        # cleanup the name for the PPA file
        repo_name = make_clean_repo_name(ppa)
        self.assertThat(
            preseed['apt']['sources'][repo_name]['key'],
            ContainsAll(ppa.key)
            )
        self.assertThat(
            preseed['apt']['sources'][repo_name]['source'],
            ContainsAll("deb %s %s main" % (ppa.url, node.distro_series))
            )

    def test_compose_curtin_archive_config_uses_multiple_ppa(self):
        node = self.make_fastpath_node('amd64')
        node.osystem = 'ubuntu'
        node.distro_series = 'xenial'
        # Create first PPA
        ppa_first = factory.make_PackageRepository(
            url='http://ppa.launchpad.net/maas/next/ubuntu',
            name='Curtin PPA', default=False, arches=['i386', 'amd64'])
        # Create second PPA
        ppa_second = factory.make_PackageRepository(
            url='http://ppa.launchpad.net/juju/devel/ubuntu',
            name='Juju PPA', default=False, arches=['i386', 'amd64'])
        self.configure_get_boot_images_for_node(node, 'xinstall')
        # compose_curtin_archive_config returns a list.
        userdata = compose_curtin_archive_config(node)
        ppas = PackageRepository.objects.get_additional_repositories(
            node.split_arch()[0])
        preseed = yaml.load(userdata[0])
        # Assert that get_additional_repositories returns 2 PPAs.
        self.assertItemsEqual(ppas, [ppa_first, ppa_second])
        # Clean up PPA name
        ppa_name = make_clean_repo_name(ppa_first)
        self.assertThat(
            preseed['apt']['sources'][ppa_name]['key'],
            ContainsAll(ppa_first.key)
            )
        self.assertThat(
            preseed['apt']['sources'][ppa_name]['source'],
            ContainsAll("deb %s %s main" % (ppa_first.url, node.distro_series))
            )
        # Clean up PPA name
        ppa_name = make_clean_repo_name(ppa_second)
        self.assertThat(
            preseed['apt']['sources'][ppa_name]['key'],
            ContainsAll(ppa_second.key)
            )
        self.assertThat(
            preseed['apt']['sources'][ppa_name]['source'],
            ContainsAll("deb %s %s main" % (
                ppa_second.url, node.distro_series)))

    def test_compose_curtin_archive_config_has_custom_repository(self):
        node = self.make_fastpath_node('i386')
        node.osystem = 'ubuntu'
        node.distro_series = 'xenial'
        factory.make_PackageRepository(
            url='http://custom.repository/ubuntu',
            name='Custom Contrail Repository', default=False,
            arches=['i386', 'amd64'])
        self.configure_get_boot_images_for_node(node, 'xinstall')
        # compose_curtin_archive_config returns a list.
        userdata = compose_curtin_archive_config(node)
        repository = PackageRepository.objects.get_additional_repositories(
            node.split_arch()[0]).first()
        preseed = yaml.load(userdata[0])
        # cleanup the name for the PPA file
        repo_name = make_clean_repo_name(repository)
        self.assertThat(
            preseed['apt']['sources'][repo_name]['key'],
            ContainsAll(repository.key)
            )
        self.assertThat(
            preseed['apt']['sources'][repo_name]['source'],
            ContainsAll("deb %s %s main" % (
                repository.url, node.distro_series)))

    def test_compose_curtin_archive_config_custom_repo_with_components(self):
        node = self.make_fastpath_node('i386')
        node.osystem = 'ubuntu'
        node.distro_series = 'xenial'
        factory.make_PackageRepository(
            url='http://custom.repository/ubuntu',
            name='Custom Contrail Repository', default=False,
            arches=['i386', 'amd64'],
            components=['main', 'universe'])
        self.configure_get_boot_images_for_node(node, 'xinstall')
        # compose_curtin_archive_config returns a list.
        userdata = compose_curtin_archive_config(node)
        repository = PackageRepository.objects.get_additional_repositories(
            node.split_arch()[0]).first()
        preseed = yaml.load(userdata[0])
        # cleanup the name for the PPA file
        repo_name = make_clean_repo_name(repository)
        components = ''
        for component in repository.components:
            components += '%s ' % component
        components = components.strip()
        self.assertThat(
            preseed['apt']['sources'][repo_name]['key'],
            ContainsAll(repository.key)
            )
        self.assertThat(
            preseed['apt']['sources'][repo_name]['source'],
            ContainsAll("deb %s %s %s" % (
                repository.url, node.distro_series, components)))

    def test_compose_curtin_archive_config_custom_repo_components_dists(self):
        node = self.make_fastpath_node('i386')
        node.osystem = 'ubuntu'
        node.distro_series = 'xenial'
        factory.make_PackageRepository(
            url='http://custom.repository/ubuntu',
            name='Custom Contrail Repository', default=False,
            arches=['i386', 'amd64'],
            distributions=['contrail'],
            components=['main', 'universe'])
        self.configure_get_boot_images_for_node(node, 'xinstall')
        # compose_curtin_archive_config returns a list.
        userdata = compose_curtin_archive_config(node)
        repository = PackageRepository.objects.get_additional_repositories(
            node.split_arch()[0]).first()
        preseed = yaml.load(userdata[0])
        # cleanup the name for the PPA file
        repo_name = make_clean_repo_name(repository)
        components = ''
        for component in repository.components:
            components += '%s ' % component
        components = components.strip()
        self.assertThat(
            preseed['apt']['sources'][repo_name]['key'],
            ContainsAll(repository.key)
            )
        self.assertThat(
            preseed['apt']['sources'][repo_name]['source'],
            ContainsAll("deb %s %s %s" % (
                repository.url, repository.distributions[0], components)))

    def test_compose_curtin_archive_config_ports_archive_for_other_arch(self):
        node = self.make_fastpath_node('ppc64el')
        node.osystem = 'ubuntu'
        main_url = PackageRepository.get_ports_archive()
        factory.make_PackageRepository(
            url=main_url, default=True, arches=['ppc64el', 'arm64'])
        self.configure_get_boot_images_for_node(node, 'xinstall')
        # compose_curtin_archive_config returns a list.
        userdata = compose_curtin_archive_config(node)
        archive = PackageRepository.objects.get_default_archive(
            node.split_arch()[0])
        self.assertEqual(
            archive.url,
            self.extract_archive_setting(userdata[0]))

    def test_compose_curtin_archive_config_main_archive_for_custom_os(self):
        node = self.make_fastpath_node('amd64')
        node.osystem = 'custom'
        main_url = 'http://us.archive.ubuntu.com/ubuntu'
        factory.make_PackageRepository(
            url=main_url, default=True, arches=['i386', 'amd64'])
        self.configure_get_boot_images_for_node(node, 'xinstall')
        # compose_curtin_archive_config returns a list.
        userdata = compose_curtin_archive_config(node)
        archive = PackageRepository.objects.get_default_archive(
            node.split_arch()[0])
        self.assertEqual(
            archive.url,
            self.extract_archive_setting(userdata[0]))

    def test_get_curtin_context(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller)
        context = get_curtin_context(node)
        self.assertItemsEqual(
            ['curtin_preseed'], context)
        self.assertIn('cloud-init', context['curtin_preseed'])

    def test_get_curtin_image_calls_get_boot_images_for(self):
        osystem = factory.make_name('os')
        series = factory.make_name('series')
        architecture = make_usable_architecture(self)
        arch, subarch = architecture.split('/')
        node = factory.make_Node_with_Interface_on_Subnet(
            osystem=osystem, distro_series=series, architecture=architecture)
        mock_get_boot_images_for = self.patch(
            preseed_module, 'get_boot_images_for')
        mock_get_boot_images_for.return_value = [
            make_rpc_boot_image(purpose='xinstall')]
        get_curtin_image(node)
        self.assertThat(
            mock_get_boot_images_for,
            MockCalledOnceWith(
                node.get_boot_primary_rack_controller(),
                osystem, arch, subarch, series))

    def test_get_curtin_image_raises_ClusterUnavailable(self):
        node = factory.make_Node_with_Interface_on_Subnet()
        self.patch(
            preseed_module,
            'get_boot_images_for').side_effect = NoConnectionsAvailable
        self.assertRaises(ClusterUnavailable, get_curtin_image, node)

    def test_get_curtin_image_raises_MissingBootImage(self):
        node = factory.make_Node()
        self.patch(
            preseed_module,
            'get_boot_images_for').return_value = []
        self.assertRaises(MissingBootImage, get_curtin_image, node)

    def test_get_curtin_image_returns_xinstall_image(self):
        node = factory.make_Node()
        other_images = [make_rpc_boot_image() for _ in range(3)]
        xinstall_image = make_rpc_boot_image(purpose='xinstall')
        images = other_images + [xinstall_image]
        self.patch(
            preseed_module,
            'get_boot_images_for').return_value = images
        self.assertEqual(xinstall_image, get_curtin_image(node))

    def test_get_curtin_installer_url_returns_url_for_tgz(self):
        osystem = make_usable_osystem(self)
        series = osystem['default_release']
        architecture = make_usable_architecture(self)
        xinstall_path = factory.make_name('xi_path')
        xinstall_type = factory.make_name('xi_type')
        cluster_ip = factory.make_ipv4_address()
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller, osystem=osystem['name'],
            architecture=architecture, distro_series=series,
            boot_cluster_ip=cluster_ip)
        arch, subarch = architecture.split('/')
        boot_image = make_rpc_boot_image(
            osystem=osystem['name'], release=series,
            architecture=arch, subarchitecture=subarch,
            purpose='xinstall', xinstall_path=xinstall_path,
            xinstall_type=xinstall_type)
        self.patch(
            preseed_module,
            'get_boot_images_for').return_value = [boot_image]

        installer_url = get_curtin_installer_url(node)
        self.assertEqual(
            '%s:http://%s:5248/images/%s/%s/%s/%s/%s/%s' % (
                xinstall_type, cluster_ip, osystem['name'], arch, subarch,
                series, boot_image['label'], xinstall_path),
            installer_url)

    def test_get_curtin_installer_url_returns_cp_for_squashfs(self):
        osystem = make_usable_osystem(self)
        series = osystem['default_release']
        architecture = make_usable_architecture(self)
        xinstall_path = factory.make_name('xi_path')
        xinstall_type = 'squashfs'
        cluster_ip = factory.make_ipv4_address()
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller, osystem=osystem['name'],
            architecture=architecture, distro_series=series,
            boot_cluster_ip=cluster_ip)
        arch, subarch = architecture.split('/')
        boot_image = make_rpc_boot_image(
            osystem=osystem['name'], release=series,
            architecture=arch, subarchitecture=subarch,
            purpose='xinstall', xinstall_path=xinstall_path,
            xinstall_type=xinstall_type)
        self.patch(
            preseed_module,
            'get_boot_images_for').return_value = [boot_image]

        installer_url = get_curtin_installer_url(node)
        self.assertEqual('cp:///media/root-ro', installer_url)

    def test_get_curtin_installer_url_fails_if_no_boot_image(self):
        osystem = make_usable_osystem(self)
        series = osystem['default_release']
        architecture = make_usable_architecture(self)
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller, osystem=osystem['name'],
            architecture=architecture, distro_series=series)
        # Make boot image that is not xinstall
        arch, subarch = architecture.split('/')
        boot_image = make_rpc_boot_image(
            osystem=osystem['name'], release=series,
            architecture=arch, subarchitecture=subarch)
        self.patch(
            preseed_module,
            'get_boot_images_for').return_value = [boot_image]

        error = self.assertRaises(
            MissingBootImage, get_curtin_installer_url, node)
        arch, subarch = architecture.split('/')
        msg = (
            "No image could be found for the given selection: "
            "os=%s, arch=%s, subarch=%s, series=%s, purpose=xinstall." % (
                osystem['name'],
                arch,
                subarch,
                node.get_distro_series(),
            ))
        self.assertIn(msg, "%s" % error)

    def test_get_curtin_installer_url_doesnt_append_on_tgz(self):
        osystem = make_usable_osystem(self)
        series = osystem['default_release']
        architecture = make_usable_architecture(self)
        xinstall_path = factory.make_name('xi_path')
        xinstall_type = 'tgz'
        cluster_ip = factory.make_ipv4_address()
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller, osystem=osystem['name'],
            architecture=architecture, distro_series=series,
            boot_cluster_ip=cluster_ip)
        arch, subarch = architecture.split('/')
        boot_image = make_rpc_boot_image(
            osystem=osystem['name'], release=series,
            architecture=arch, subarchitecture=subarch,
            purpose='xinstall', xinstall_path=xinstall_path,
            xinstall_type=xinstall_type)
        self.patch(
            preseed_module,
            'get_boot_images_for').return_value = [boot_image]

        installer_url = get_curtin_installer_url(node)
        self.assertEqual(
            'http://%s:5248/images/%s/%s/%s/%s/%s/%s' % (
                cluster_ip, osystem['name'], arch, subarch,
                series, boot_image['label'], xinstall_path),
            installer_url)

    def test_get_preseed_type_for_commissioning(self):
        node = factory.make_Node(status=NODE_STATUS.COMMISSIONING)
        self.assertEqual(
            PRESEED_TYPE.COMMISSIONING, get_preseed_type_for(node))

    def test_get_preseed_type_for_disk_erasing(self):
        node = factory.make_Node(status=NODE_STATUS.DISK_ERASING)
        self.assertEqual(
            PRESEED_TYPE.COMMISSIONING, get_preseed_type_for(node))

    def test_get_preseed_type_for_curtin(self):
        node = factory.make_Node(status=NODE_STATUS.DEPLOYING)
        self.configure_get_boot_images_for_node(node, 'xinstall')
        self.assertEqual(
            PRESEED_TYPE.CURTIN, get_preseed_type_for(node))

    def test_get_preseed_type_for_poweroff(self):
        # A 'ready' node isn't supposed to be powered on and thus
        # will get a 'commissioning' preseed in order to be powered
        # down.
        node = factory.make_Node(status=NODE_STATUS.READY)
        self.assertEqual(
            PRESEED_TYPE.COMMISSIONING, get_preseed_type_for(node))


class TestPreseedMethods(
        PreseedRPCMixin, BootImageHelperMixin, MAASTransactionServerTestCase):
    """Tests for `get_enlist_preseed` and `get_preseed`.

    These tests check that the preseed templates render and 'look right'.
    """

    def test_get_preseed_returns_curtin_preseed(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller,
            status=NODE_STATUS.DEPLOYING)
        self.configure_get_boot_images_for_node(node, 'xinstall')
        preseed = get_preseed(node)
        curtin_url = reverse('curtin-metadata')
        self.assertIn(curtin_url.encode("utf-8"), preseed)

    def test_get_enlist_preseed_returns_enlist_preseed(self):
        preseed = get_enlist_preseed()
        self.assertTrue(preseed.startswith(b'#cloud-config'))

    def test_get_preseed_returns_commissioning_preseed(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller,
            status=NODE_STATUS.COMMISSIONING)
        preseed = get_preseed(node)
        self.assertIn(b'#cloud-config', preseed)

    def test_get_preseed_returns_commissioning_preseed_for_disk_erasing(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller,
            status=NODE_STATUS.DISK_ERASING)
        preseed = get_preseed(node)
        self.assertIn(b'#cloud-config', preseed)


class TestPreseedURLs(
        PreseedRPCMixin, BootImageHelperMixin, MAASTransactionServerTestCase):
    """Tests for functions that return preseed URLs."""

    def test_compose_enlistment_preseed_url_links_to_enlistment_preseed(self):
        response = self.client.get(compose_enlistment_preseed_url())
        self.assertEqual(
            (http.client.OK, get_enlist_preseed()),
            (response.status_code, response.content))

    def test_compose_enlistment_preseed_url_returns_absolute_link(self):
        maas_url = factory.make_simple_http_url(path='')
        self.useFixture(RegionConfigurationFixture(maas_url=maas_url))

        self.assertThat(
            compose_enlistment_preseed_url(), StartsWith(maas_url))

    def test_compose_enlistment_preseed_url_returns_abs_link_wth_nodegrp(self):
        maas_url = factory.make_simple_http_url(path='')
        self.useFixture(RegionConfigurationFixture(maas_url=maas_url))
        rack_controller = factory.make_RackController(url=maas_url)

        self.assertThat(
            compose_enlistment_preseed_url(rack_controller=rack_controller),
            StartsWith(maas_url))

    def test_compose_preseed_url_links_to_preseed_for_node(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            primary_rack=self.rpc_rack_controller)
        self.configure_get_boot_images_for_node(node, 'install')
        response = self.client.get(
            compose_preseed_url(node, self.rpc_rack_controller))
        self.assertEqual(
            (http.client.OK, get_preseed(node)),
            (response.status_code, response.content))

    def test_compose_preseed_url_returns_absolute_link(self):
        self.assertThat(
            compose_preseed_url(
                factory.make_Node_with_Interface_on_Subnet(),
                self.rpc_rack_controller),
            StartsWith('http://'))