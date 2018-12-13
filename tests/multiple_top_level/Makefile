all: lib/b/libb.so lib/a/liba.so


lib/b/libb.so: lib/b/b.c
	gcc -fPIC -shared -o lib/b/libb.so lib/b/b.c


lib/a/liba.so: lib/b/libb.so lib/a/a.c
	gcc -fPIC -shared -o lib/a/liba.so -Ilib/b -Llib/b -lb lib/a/a.c
