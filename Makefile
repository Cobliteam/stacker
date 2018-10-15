PYTHON := $(shell which python)
PYTHON_MAJOR_VERSION := $(shell $(PYTHON) -c 'import sys; sys.stdout.write(str(sys.version_info.major))')

.PHONY: build flake8 typecheck lint test-functional test-unit test test-all \
		clean apidocs

build:
	docker build -t remind101/stacker .

flake8:
	flake8 stacker

typecheck:
ifeq ($(PYTHON_MAJOR_VERSION), 2)
	tox -e mypy -- --py2 --python-executable $(PYTHON) -p stacker
else
	mypy -p stacker
endif

lint: | flake8 typecheck

test-functional:
	$(MAKE) -C tests

test-unit:
	AWS_DEFAULT_REGION=us-east-1 \
	AWS_ACCESS_KEY_ID=bad AWS_SECRET_ACCESS_KEY=bad \
	nosetests

test: | lint test-unit

test-all:
	tox --skip-missing-interpreters=true test

clean:
	rm -rf .egg stacker.egg-info

apidocs:
	sphinx-apidoc --force -o docs/api stacker
