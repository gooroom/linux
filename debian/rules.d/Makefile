DATA = \
	Kbuild \
	Makefile \

SUBDIRS = \
	scripts \
	tools

DEB_HOST_ARCH := $(shell dpkg-architecture -qDEB_HOST_ARCH)
ifneq ($(filter powerpc powerpcspe ppc64,$(DEB_HOST_ARCH)),)
	SUBDIRS += arch/powerpc/boot
endif

include $(top_rulesdir)/Makefile.inc
