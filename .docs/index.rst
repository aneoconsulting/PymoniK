PymoniK
=======

A dead-simple Python SDK for `ArmoniK <https://github.com/aneoconsulting/ArmoniK>`_.

PymoniK turns a regular Python function into a remote task with a single
decorator. Tasks compose into pipelines via plain function calls; the
SDK takes care of submission, data dependencies, retries, and result
delivery. The same code runs locally for tests and on a cluster of
hundreds of pods for production.

.. code-block:: python

    from pymonik import PymonikClient, task

    @task
    def add(a: int, b: int) -> int:
        return a + b

    @task
    def total(xs: list[int]) -> int:
        return sum(xs)

    with PymonikClient() as client:
        with client.session(partition="pymonik") as s:
            parts = add.map(range(16), range(1, 17))
            print(total.spawn(parts).result(timeout=60))

.. toctree::
   :maxdepth: 2
   :caption: First steps

   introduction
   getting-started
   important-considerations

.. toctree::
   :maxdepth: 2
   :caption: Guides

   guides/runtime-environment
   guides/blobs-and-materialize
   guides/sub-tasking-and-multi-output
   guides/multi-partition
   guides/retries
   guides/local-testing
   guides/observability
   guides/async
   guides/worker-images
   guides/custom-worker

.. toctree::
   :maxdepth: 2
   :caption: Examples

   examples/monte_carlo
   examples/raytracing
   examples/pong_training
   examples/pricing_workflows

.. toctree::
   :maxdepth: 2
   :caption: Development

   development/development
   development/contribution
