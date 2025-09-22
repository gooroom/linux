#!/usr/bin/python3

import sys
import locale
import os
import os.path
import subprocess
import re

from debian_linux import config
from debian_linux.debian import PackageRelation, \
    PackageRelationEntry, PackageRelationGroup, VersionLinux, BinaryPackage, \
    restriction_requires_profile
from debian_linux.gencontrol import Gencontrol as Base, \
    iter_featuresets, iter_flavours, add_package_build_restriction
from debian_linux.utils import Templates

locale.setlocale(locale.LC_CTYPE, "C.UTF-8")


class Gencontrol(Base):
    config_schema = {
        'abi': {
            'ignore-changes': config.SchemaItemList(),
        },
        'build': {
            'signed-code': config.SchemaItemBoolean(),
            'vdso': config.SchemaItemBoolean(),
        },
        'description': {
            'parts': config.SchemaItemList(),
        },
        'image': {
            'bootloaders': config.SchemaItemList(),
            'configs': config.SchemaItemList(),
            'initramfs-generators': config.SchemaItemList(),
            'check-size': config.SchemaItemInteger(),
            'check-size-with-dtb': config.SchemaItemBoolean(),
            'check-uncompressed-size': config.SchemaItemInteger(),
            'depends': config.SchemaItemList(','),
            'provides': config.SchemaItemList(','),
            'suggests': config.SchemaItemList(','),
            'recommends': config.SchemaItemList(','),
            'conflicts': config.SchemaItemList(','),
            'breaks': config.SchemaItemList(','),
        },
        'relations': {
        },
        'packages': {
            'docs': config.SchemaItemBoolean(),
            'installer': config.SchemaItemBoolean(),
            'libc-dev': config.SchemaItemBoolean(),
            'meta': config.SchemaItemBoolean(),
            'tools-unversioned': config.SchemaItemBoolean(),
            'tools-versioned': config.SchemaItemBoolean(),
            'source': config.SchemaItemBoolean(),
        }
    }

    env_flags = [
        ('DEBIAN_KERNEL_DISABLE_INSTALLER', 'disable_installer', 'installer modules'),
        ('DEBIAN_KERNEL_DISABLE_SIGNED', 'disable_signed', 'signed code'),
    ]

    def __init__(self, config_dirs=["debian/config", "debian/config.local"],
                 template_dirs=["debian/templates"]):
        super(Gencontrol, self).__init__(
            config.ConfigCoreHierarchy(self.config_schema, config_dirs),
            Templates(template_dirs),
            VersionLinux)
        self.process_changelog()
        self.config_dirs = config_dirs

        for env, attr, desc in self.env_flags:
            setattr(self, attr, False)
            if os.getenv(env):
                if self.changelog[0].distribution == 'UNRELEASED':
                    import warnings
                    warnings.warn(f'Disable {desc} on request ({env} set)')
                    setattr(self, attr, True)
                else:
                    raise RuntimeError(
                        f'Unable to disable {desc} in release build ({env} set)')

    def _setup_makeflags(self, names, makeflags, data):
        for src, dst, optional in names:
            if src in data or not optional:
                makeflags[dst] = data[src]

    def do_main_setup(self, vars, makeflags, extra):
        super(Gencontrol, self).do_main_setup(vars, makeflags, extra)
        makeflags.update({
            'VERSION': self.version.linux_version,
            'UPSTREAMVERSION': self.version.linux_upstream,
            'ABINAME': self.abiname_version + self.abiname_part,
            'SOURCEVERSION': self.version.complete,
        })
        makeflags['SOURCE_BASENAME'] = vars['source_basename']
        makeflags['SOURCE_SUFFIX'] = vars['source_suffix']

        # Prepare to generate debian/tests/control
        self.tests_control = self.templates.get_tests_control('main.tests-control', vars)
        self.tests_control_image = None
        self.tests_control_headers = None

        self.installer_packages = {}

        if not self.disable_installer and self.config.merge('packages').get('installer', True):
            # Add udebs using kernel-wedge
            kw_env = os.environ.copy()
            kw_env['KW_DEFCONFIG_DIR'] = 'debian/installer'
            kw_env['KW_CONFIG_DIR'] = 'debian/installer'
            kw_proc = subprocess.Popen(
                ['kernel-wedge', 'gen-control', vars['abiname']],
                stdout=subprocess.PIPE,
                text=True,
                env=kw_env)
            udeb_packages = BinaryPackage.read_rfc822(kw_proc.stdout)
            kw_proc.wait()
            if kw_proc.returncode != 0:
                raise RuntimeError('kernel-wedge exited with code %d' %
                                   kw_proc.returncode)

            # All architectures that have some installer udebs
            arches = set()
            for package in udeb_packages:
                arches.update(package['Architecture'])

            # Code-signing status for those architectures
            # If we're going to build signed udebs later, don't actually
            # generate udebs.  Just test that we *can* build, so we find
            # configuration errors before building linux-signed.
            build_signed = {}
            for arch in arches:
                if not self.disable_signed:
                    build_signed[arch] = self.config.merge('build', arch) \
                                                    .get('signed-code', False)
                else:
                    build_signed[arch] = False

            for package in udeb_packages:
                # kernel-wedge currently chokes on Build-Profiles so add it now
                if any(build_signed[arch] for arch in package['Architecture']):
                    assert all(build_signed[arch]
                               for arch in package['Architecture'])
                    # XXX This is a hack to exclude the udebs from
                    # the package list while still being able to
                    # convince debhelper and kernel-wedge to go
                    # part way to building them.
                    package['Build-Profiles'] = (
                        '<pkg.linux.udeb-unsigned-test-build !noudeb !stage1'
                        ' !pkg.linux.nokernel !pkg.linux.quick>')
                else:
                    package['Build-Profiles'] = (
                        '<!noudeb !stage1 !pkg.linux.nokernel !pkg.linux.quick>')

                for arch in package['Architecture']:
                    self.installer_packages.setdefault(arch, []) \
                                           .append(package)

    def do_main_makefile(self, makeflags, extra):
        for featureset in iter_featuresets(self.config):
            makeflags_featureset = makeflags.copy()
            makeflags_featureset['FEATURESET'] = featureset

            self.makefile.add_rules(f'source_{featureset}',
                                    'source', makeflags_featureset)
            self.makefile.add_deps('source', [f'source_{featureset}'])

        makeflags = makeflags.copy()
        makeflags['ALL_FEATURESETS'] = ' '.join(iter_featuresets(self.config))
        super().do_main_makefile(makeflags, extra)

    def do_main_packages(self, vars, makeflags, extra):
        self.bundle.add('main', ('real', ), makeflags, vars)

        # Only build the metapackages if their names won't exactly match
        # the packages they depend on
        do_meta = self.config.merge('packages').get('meta', True) \
            and vars['source_suffix'] != '-' + vars['version']

        if self.config.merge('packages').get('docs', True):
            self.bundle.add('docs', ('real', ), makeflags, vars)
            if do_meta:
                self.bundle.add('docs.meta', ('real', ), makeflags, vars)
        if self.config.merge('packages').get('source', True):
            self.bundle.add('sourcebin', ('real', ), makeflags, vars)
            if do_meta:
                self.bundle.add('sourcebin.meta', ('real', ), makeflags, vars)

    def do_indep_featureset_setup(self, vars, makeflags, featureset, extra):
        makeflags['LOCALVERSION'] = vars['localversion']
        kernel_arches = set()
        for arch in iter(self.config['base', ]['arches']):
            if self.config.get_merge('base', arch, featureset, None,
                                     'flavours'):
                kernel_arches.add(self.config['base', arch]['kernel-arch'])
        makeflags['ALL_KERNEL_ARCHES'] = ' '.join(sorted(list(kernel_arches)))

        vars['featureset_desc'] = ''
        if featureset != 'none':
            desc = self.config[('description', None, featureset)]
            desc_parts = desc['parts']
            vars['featureset_desc'] = (' with the %s featureset' %
                                       desc['part-short-%s' % desc_parts[0]])

    def do_indep_featureset_packages(self, featureset,
                                     vars, makeflags, extra):
        self.bundle.add('headers.featureset', (featureset, 'real'), makeflags, vars)

    arch_makeflags = (
        ('kernel-arch', 'KERNEL_ARCH', False),
    )

    def do_arch_setup(self, vars, makeflags, arch, extra):
        config_base = self.config.merge('base', arch)

        self._setup_makeflags(self.arch_makeflags, makeflags, config_base)

        try:
            gnu_type = subprocess.check_output(
                ['dpkg-architecture', '-f', '-a', arch,
                 '-q', 'DEB_HOST_GNU_TYPE'],
                stderr=subprocess.DEVNULL,
                encoding='utf-8')
        except subprocess.CalledProcessError:
            # This sometimes happens for the newest ports :-/
            print('W: Unable to get GNU type for %s' % arch, file=sys.stderr)
        else:
            vars['gnu-type-package'] = gnu_type.strip().replace('_', '-')

    def do_arch_packages(self, arch, vars, makeflags,
                         extra):
        if not self.disable_signed:
            build_signed = self.config.merge('build', arch) \
                                      .get('signed-code', False)
        else:
            build_signed = False

        udeb_packages = self.installer_packages.get(arch, [])
        if udeb_packages:
            makeflags_local = makeflags.copy()
            makeflags_local['PACKAGE_NAMES'] = ' '.join(p['Package'] for p in udeb_packages)

            for package in udeb_packages:
                package.meta['rules-target'] = build_signed and 'udeb_test' or 'udeb'

            self.bundle.add_packages(
                udeb_packages,
                (arch, 'real'),
                makeflags_local, arch=arch, check_packages=not build_signed,
            )

        if build_signed:
            self.bundle.add('signed-template', (arch, 'real'), makeflags, vars, arch=arch)

        if self.config.merge('packages').get('libc-dev', True):
            self.bundle.add('libc-dev', (arch, 'real'), makeflags, vars)

        if self.config['base', arch].get('featuresets') and \
           self.config.merge('packages').get('source', True):
            self.bundle.add('config', (arch, 'real'), makeflags, vars)

        if self.config.merge('packages').get('tools-unversioned', True):
            self.bundle.add('tools-unversioned', (arch, 'real'), makeflags, vars)

        if self.config.merge('packages').get('tools-versioned', True):
            self.bundle.add('tools-versioned', (arch, 'real'), makeflags, vars)

    def do_featureset_setup(self, vars, makeflags, arch, featureset, extra):
        vars['localversion_headers'] = vars['localversion']
        makeflags['LOCALVERSION_HEADERS'] = vars['localversion_headers']

        self.default_flavour = self.config.merge('base', arch, featureset) \
                                          .get('default-flavour')
        if self.default_flavour is not None:
            if featureset != 'none':
                raise RuntimeError("default-flavour set for %s %s,"
                                   " but must only be set for featureset none"
                                   % (arch, featureset))
            if self.default_flavour \
               not in iter_flavours(self.config, arch, featureset):
                raise RuntimeError("default-flavour %s for %s %s does not exist"
                                   % (self.default_flavour, arch, featureset))

        self.quick_flavour = self.config.merge('base', arch, featureset) \
                                        .get('quick-flavour')

    flavour_makeflags_base = (
        ('compiler', 'COMPILER', False),
        ('compiler-filename', 'COMPILER', True),
        ('kernel-arch', 'KERNEL_ARCH', False),
        ('cflags', 'KCFLAGS', True),
        ('override-host-type', 'OVERRIDE_HOST_TYPE', True),
        ('cross-compile-compat', 'CROSS_COMPILE_COMPAT', True),
    )

    flavour_makeflags_build = (
        ('image-file', 'IMAGE_FILE', True),
    )

    flavour_makeflags_image = (
        ('install-stem', 'IMAGE_INSTALL_STEM', True),
    )

    flavour_makeflags_other = (
        ('localversion', 'LOCALVERSION', False),
        ('localversion-image', 'LOCALVERSION_IMAGE', True),
    )

    def do_flavour_setup(self, vars, makeflags, arch, featureset, flavour,
                         extra):
        config_base = self.config.merge('base', arch, featureset, flavour)
        config_build = self.config.merge('build', arch, featureset, flavour)
        config_description = self.config.merge('description', arch, featureset,
                                               flavour)
        config_image = self.config.merge('image', arch, featureset, flavour)

        vars['flavour'] = vars['localversion'][1:]
        vars['class'] = config_description['hardware']
        vars['longclass'] = (config_description.get('hardware-long')
                             or vars['class'])

        vars['localversion-image'] = vars['localversion']
        override_localversion = config_image.get('override-localversion', None)
        if override_localversion is not None:
            vars['localversion-image'] = (vars['localversion_headers'] + '-'
                                          + override_localversion)
        vars['image-stem'] = config_image.get('install-stem')

        self._setup_makeflags(self.flavour_makeflags_base, makeflags,
                              config_base)
        self._setup_makeflags(self.flavour_makeflags_build, makeflags,
                              config_build)
        self._setup_makeflags(self.flavour_makeflags_image, makeflags,
                              config_image)
        self._setup_makeflags(self.flavour_makeflags_other, makeflags, vars)

    def do_flavour_packages(self, arch, featureset,
                            flavour, vars, makeflags, extra):
        ruleid = (arch, featureset, flavour, 'real')

        packages_headers = (
            self.bundle.add('headers', ruleid, makeflags, vars, arch=arch)
        )
        assert len(packages_headers) == 1

        do_meta = self.config.merge('packages').get('meta', True)
        config_entry_base = self.config.merge('base', arch, featureset,
                                              flavour)
        config_entry_build = self.config.merge('build', arch, featureset,
                                               flavour)
        config_entry_description = self.config.merge('description', arch,
                                                     featureset, flavour)
        config_entry_relations = self.config.merge('relations', arch,
                                                   featureset, flavour)

        def config_entry_image(key, *args, **kwargs):
            return self.config.get_merge(
                'image', arch, featureset, flavour, key, *args, **kwargs)

        compiler = config_entry_base.get('compiler', 'gcc')

        # Work out dependency from linux-headers to compiler.  Drop
        # dependencies for cross-builds.  Strip any remaining
        # restrictions, as they don't apply to binary Depends.
        relations_compiler_headers = PackageRelation(
            self.substitute(config_entry_relations.get('headers%' + compiler)
                            or config_entry_relations.get(compiler), vars))
        relations_compiler_headers = PackageRelation(
            PackageRelationGroup(
                entry for entry in group
                if not restriction_requires_profile(entry.restrictions,
                                                    'cross'))
            for group in relations_compiler_headers)
        for group in relations_compiler_headers:
            for entry in group:
                entry.restrictions = []

        relations_compiler_build_dep = PackageRelation(
            self.substitute(config_entry_relations[compiler], vars))
        for group in relations_compiler_build_dep:
            for item in group:
                item.arches = [arch]
        self.packages['source']['Build-Depends-Arch'].extend(
            relations_compiler_build_dep)

        packages_own = []

        if not self.disable_signed:
            build_signed = config_entry_build.get('signed-code')
        else:
            build_signed = False

        vars.setdefault('desc', None)

        package_image = (
            self.bundle.add(build_signed and 'image-unsigned' or 'image',
                            ruleid, makeflags, vars, arch=arch)
        )[0]
        makeflags['IMAGE_PACKAGE_NAME'] = package_image['Package']

        for field in ('Depends', 'Provides', 'Suggests', 'Recommends',
                      'Conflicts', 'Breaks'):
            package_image.setdefault(field).extend(PackageRelation(
                config_entry_image(field.lower(), None),
                override_arches=(arch,)))

        generators = config_entry_image('initramfs-generators')
        group = PackageRelationGroup()
        for i in generators:
            i = config_entry_relations.get(i, i)
            group.append(i)
            a = PackageRelationEntry(i)
            if a.operator is not None:
                a.operator = -a.operator
                package_image['Breaks'].append(PackageRelationGroup([a]))
        for item in group:
            item.arches = [arch]
        package_image['Depends'].append(group)

        bootloaders = config_entry_image('bootloaders', None)
        if bootloaders:
            group = PackageRelationGroup()
            for i in bootloaders:
                i = config_entry_relations.get(i, i)
                group.append(i)
                a = PackageRelationEntry(i)
                if a.operator is not None:
                    a.operator = -a.operator
                    package_image['Breaks'].append(PackageRelationGroup([a]))
            for item in group:
                item.arches = [arch]
            package_image['Suggests'].append(group)

        desc_parts = self.config.get_merge('description', arch, featureset,
                                           flavour, 'parts')
        if desc_parts:
            # XXX: Workaround, we need to support multiple entries of the same
            # name
            parts = list(set(desc_parts))
            parts.sort()
            desc = package_image['Description']
            for part in parts:
                desc.append(config_entry_description['part-long-' + part])
                desc.append_short(config_entry_description
                                  .get('part-short-' + part, ''))

        packages_headers[0]['Depends'].extend(relations_compiler_headers)
        packages_own.append(package_image)
        packages_own.extend(packages_headers)
        if extra.get('headers_arch_depends'):
            extra['headers_arch_depends'].append('%s (= ${binary:Version})' %
                                                 packages_own[-1]['Package'])

        # The image meta-packages will depend on signed linux-image
        # packages where applicable, so should be built from the
        # signed source packages The header meta-packages will also be
        # built along with the signed packages, to create a dependency
        # relationship that ensures src:linux and src:linux-signed-*
        # transition to testing together.
        if do_meta and not build_signed:
            packages_meta = (
                self.bundle.add('image.meta', ruleid, makeflags, vars, arch=arch)
            )
            assert len(packages_meta) == 1
            packages_meta += (
                self.bundle.add('headers.meta', ruleid, makeflags, vars, arch=arch)
            )
            assert len(packages_meta) == 2

            if flavour == self.default_flavour \
               and not self.vars['source_suffix']:
                packages_meta[0].setdefault('Provides') \
                                .append('linux-image-generic')
                packages_meta[1].setdefault('Provides') \
                                .append('linux-headers-generic')

            packages_own.extend(packages_meta)

        if config_entry_build.get('vdso', False):
            makeflags['VDSO'] = True

        packages_own.extend(
            self.bundle.add('image-dbg', ruleid, makeflags, vars, arch=arch)
        )
        if do_meta:
            packages_own.extend(
                self.bundle.add('image-dbg.meta', ruleid, makeflags, vars, arch=arch)
            )

        # In a quick build, only build the quick flavour (if any).
        if flavour != self.quick_flavour:
            for package in packages_own:
                add_package_build_restriction(package, '!pkg.linux.quick')

        # Make sure signed-template is build after linux
        if build_signed:
            self.makefile.add_deps(f'build-arch_{arch}_real_signed-template',
                                   [f'build-arch_{arch}_{featureset}_{flavour}_real'])
            self.makefile.add_deps(f'binary-arch_{arch}_real_signed-template',
                                   [f'binary-arch_{arch}_{featureset}_{flavour}_real'])

        # Make sure udeb is build after linux
        self.makefile.add_deps(f'build-arch_{arch}_real_udeb',
                               [f'build-arch_{arch}_{featureset}_{flavour}_real'])
        self.makefile.add_deps(f'binary-arch_{arch}_real_udeb',
                               [f'binary-arch_{arch}_{featureset}_{flavour}_real'])

        tests_control = self.templates.get_tests_control('image.tests-control', vars)[0]
        tests_control['Depends'].append(
            PackageRelationGroup(package_image['Package'],
                                 override_arches=(arch,)))
        if self.tests_control_image:
            self.tests_control_image['Depends'].extend(
                tests_control['Depends'])
        else:
            self.tests_control_image = tests_control
            self.tests_control.append(tests_control)

        if flavour == (self.quick_flavour or self.default_flavour):
            if not self.tests_control_headers:
                self.tests_control_headers = \
                        self.templates.get_tests_control('headers.tests-control', vars)[0]
                self.tests_control.append(self.tests_control_headers)
            self.tests_control_headers['Architecture'].add(arch)
            self.tests_control_headers['Depends'].append(
                PackageRelationGroup(packages_headers[0]['Package'],
                                     override_arches=(arch,)))

        def get_config(*entry_name):
            entry_real = ('image',) + entry_name
            entry = self.config.get(entry_real, None)
            if entry is None:
                return None
            return entry.get('configs', None)

        def check_config_default(fail, f):
            for d in self.config_dirs[::-1]:
                f1 = d + '/' + f
                if os.path.exists(f1):
                    return [f1]
            if fail:
                raise RuntimeError("%s unavailable" % f)
            return []

        def check_config_files(files):
            ret = []
            for f in files:
                for d in self.config_dirs[::-1]:
                    f1 = d + '/' + f
                    if os.path.exists(f1):
                        ret.append(f1)
                        break
                else:
                    raise RuntimeError("%s unavailable" % f)
            return ret

        def check_config(default, fail, *entry_name):
            configs = get_config(*entry_name)
            if configs is None:
                return check_config_default(fail, default)
            return check_config_files(configs)

        kconfig = check_config('config', True)
        # XXX: We have no way to override kernelarch-X configs
        kconfig.extend(check_config_default(False,
                       "kernelarch-%s/config" % config_entry_base['kernel-arch']))
        kconfig.extend(check_config("%s/config" % arch, True, arch))
        kconfig.extend(check_config("%s/config.%s" % (arch, flavour), False,
                                    arch, None, flavour))
        kconfig.extend(check_config("featureset-%s/config" % featureset, False,
                                    None, featureset))
        kconfig.extend(check_config("%s/%s/config" % (arch, featureset), False,
                                    arch, featureset))
        kconfig.extend(check_config("%s/%s/config.%s" %
                                    (arch, featureset, flavour), False,
                                    arch, featureset, flavour))
        makeflags['KCONFIG'] = ' '.join(kconfig)
        makeflags['KCONFIG_OPTIONS'] = ''
        #if build_signed:
        #    makeflags['KCONFIG_OPTIONS'] += ' -o SECURITY_LOCKDOWN_LSM=y -o MODULE_SIG=y'
        # Add "salt" to fix #872263
        makeflags['KCONFIG_OPTIONS'] += \
            ' -o "BUILD_SALT=\\"%(abiname)s%(localversion)s\\""' % vars
        #if config_entry_build.get('trusted-certs'):
        #    makeflags['KCONFIG_OPTIONS'] += \
        #        f' -o "SYSTEM_TRUSTED_KEYS=\\"${{CURDIR}}/{config_entry_build["trusted-certs"]}\\""'

        merged_config = ('debian/build/config.%s_%s_%s' %
                         (arch, featureset, flavour))
        self.makefile.add_cmds(merged_config,
                               ["$(MAKE) -f debian/rules.real %s %s" %
                                (merged_config, makeflags)])

    # Generate a unique kernel ABI name suffix following the old
    # (pre-trixie) format
    def _generate_abiname_part(self):
        # Start with 38 for 6.1.147-1 (the last manually set ABI
        # version) and add 1 for every non-backport changelog entry
        # after that.  Also count backport entries since the last
        # non-backport, in case we need further disambiguation.
        abiname_major = 0
        backport_count = 0
        for entry in self.changelog:
            if '~' in entry.version.revision:
                if abiname_major == 0:
                    backport_count += 1
            elif str(entry.version) == '6.1.147-1':
                abiname_major += 38
                break
            else:
                abiname_major += 1

        # Select an appropriate prefix for the target release
        for release_name, release_prefix in [
                ('UNRELEASED', ''),
                ('gooroom-4.0', ''),
                ('bookworm',   ''),
                ('bullseye',   '0.deb11.'),
                ('buster',     '0.deb10.'),
                ('stretch',    '0.deb9.'),
        ]:
            if self.changelog[0].distribution.startswith(release_name):
                break
        else:
            assert False, f'cannot generate ABI name for {self.changelog[0].distribution}'

        # Paste that all together
        result = f'-{release_prefix}{abiname_major}'
        if backport_count > 1:
            result += f'.{backport_count - 1}'
        return result

    def process_changelog(self):
        version = self.version = self.changelog[0].version
        try:
            self.abiname_part = '-%s' % self.config['abi', ]['abiname']
        except KeyError:
            self.abiname_part = self._generate_abiname_part()
        # We need to keep at least three version components to avoid
        # userland breakage (e.g. #742226, #745984).
        self.abiname_version = re.sub(r'^(\d+\.\d+)(?=-|$)', r'\1.0',
                                      self.version.linux_version)
        self.vars = {
            'upstreamversion': self.version.linux_upstream,
            'version': self.version.linux_version,
            'source_basename': re.sub(r'-[\d.]+$', '',
                                      self.changelog[0].source),
            'source_upstream': self.version.upstream,
            'source_package': self.changelog[0].source,
            'abiname': self.abiname_version + self.abiname_part,
        }
        self.vars['source_suffix'] = \
            self.changelog[0].source[len(self.vars['source_basename']):]
        self.config['version', ] = {'source': self.version.complete,
                                    'upstream': self.version.linux_upstream,
                                    'abiname_base': self.abiname_version,
                                    'abiname': (self.abiname_version
                                                + self.abiname_part)}

        distribution = self.changelog[0].distribution
        if distribution in ('unstable', ):
            if version.linux_revision_experimental or \
               version.linux_revision_backports or \
               version.linux_revision_other:
                raise RuntimeError("Can't upload to %s with a version of %s" %
                                   (distribution, version))
        if distribution in ('experimental', ):
            if not version.linux_revision_experimental:
                raise RuntimeError("Can't upload to %s with a version of %s" %
                                   (distribution, version))
        if distribution.endswith('-security') or distribution.endswith('-lts'):
            if version.linux_revision_backports or \
               version.linux_revision_other:
                raise RuntimeError("Can't upload to %s with a version of %s" %
                                   (distribution, version))
        if distribution.endswith('-backports'):
            if not version.linux_revision_backports:
                raise RuntimeError("Can't upload to %s with a version of %s" %
                                   (distribution, version))

    def write(self):
        self.write_config()
        super().write()
        self.write_tests_control()

    def write_config(self):
        f = open("debian/config.defines.dump", 'wb')
        self.config.dump(f)
        f.close()

    def write_tests_control(self):
        self.write_rfc822(open("debian/tests/control", 'w'),
                          self.tests_control)


if __name__ == '__main__':
    Gencontrol()()
