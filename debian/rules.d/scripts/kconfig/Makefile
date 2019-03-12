PROGS = conf

include $(top_rulesdir)/Makefile.inc

conf: conf.o confdata.o expr.o symbol.o preprocess.o zconf.lex.o zconf.tab.o

zconf.lex.o: zconf.tab.h

%.lex.c: %.l
	flex -o$@ -L $<

%.tab.c: %.y
	bison -o$@ -t -l $<

%.tab.h: %.y
	bison -o/dev/null --defines=$@ -t -l $<
