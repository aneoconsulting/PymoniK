# Distributed raytracing

Raytracing renders an image by tracing the path of light rays through
a 3D scene. It's computationally expensive — every pixel needs many
ray–object intersection tests — and embarrassingly parallel: every
pixel can be computed independently.

This example renders an image by splitting it into horizontal tiles
and rendering each tile as a separate task on the cluster.

The full source lives at `examples/raytracing.py`.

## Approach

1. Define the scene (objects, lights, camera) on the client.
2. Slice the image into tiles (one task per tile).
3. Submit all tiles via `Task.starmap` (we have arg tuples already).
4. Each task computes its tile's pixels.
5. Client collects the tile results and assembles the final image.

## Prerequisites

```sh
uv add pymonik Pillow
```

`Pillow` is for image assembly on the client.

## The render task

```python
from pymonik import task

@task
def render_tile(
    y_start: int,
    y_end: int,
    image_width: int,
    image_height: int,
    camera,
    scene,
) -> tuple[int, list[tuple[int, int, int]]]:
    """Render rows [y_start, y_end) of the final image.

    `camera` and `scene` are arbitrary Python objects. PymoniK
    cloudpickles them; the worker reconstructs and uses them as if
    they were local. Both must be importable on the worker (the
    classes — not the instances — need to live in modules the worker
    can import).
    """
    pixels: list[tuple[int, int, int]] = []
    for y in range(y_start, y_end):
        for x in range(image_width):
            u = (x + 0.5) / image_width
            v = (image_height - 1 - y + 0.5) / image_height
            ray = camera.get_ray(u, v)
            color = trace_ray(ray, scene)
            pixels.append(color.to_rgb())
    return y_start, pixels
```

The function returns the tile's start row and its pixel list — the
client uses the start row to know where each tile goes in the final
image.

## Submitting and assembling

```python
import math, os
from PIL import Image
from pymonik import PymonikClient


def main() -> None:
    image_width  = 600
    image_height = 400
    num_tasks    = int(os.getenv("NUM_RAYTRACING_TASKS", "16"))
    rows_per     = math.ceil(image_height / num_tasks)

    camera = build_camera(image_width, image_height)
    scene  = build_scene()

    task_args = []
    for i in range(num_tasks):
        y_start = i * rows_per
        y_end   = min((i + 1) * rows_per, image_height)
        if y_start >= y_end:
            continue
        task_args.append(
            (y_start, y_end, image_width, image_height, camera, scene)
        )

    with PymonikClient() as client:
        with client.session(partition="pymonik") as s:
            tiles = render_tile.starmap(task_args)
            results = tiles.results(timeout=600)

    image = Image.new("RGB", (image_width, image_height))
    flat: list[tuple[int, int, int]] = [(255, 0, 255)] * (image_width * image_height)
    for y_start, pixels in results:
        for j, color in enumerate(pixels):
            x = j % image_width
            y = y_start + j // image_width
            flat[y * image_width + x] = color
    image.putdata(flat)
    image.save("raytraced.png")


if __name__ == "__main__":
    main()
```

## Things worth noting

**Auto-spill on the camera and scene.** The camera and scene objects
are passed to every task. If their cloudpickled size exceeds 256 KiB
(the default `spill_threshold`), PymoniK uploads them once as blobs
and the workers download them — instead of inlining them into every
task's payload. You don't have to do anything for this to happen;
see [Blobs and Materialize](../guides/blobs-and-materialize.md) for
the explicit form.

**Multi-file project.** `Vec3`, `Camera`, `Scene`, `trace_ray`, etc.
are typically in their own modules in a real raytracing project.
Workers need to be able to import those modules. Either:

- Bake the project into your worker image (production), or
- Call `cloudpickle.register_pickle_by_value(my_raytracer_pkg)` at
  the client's entrypoint (fast iteration). See
  [Important considerations](../important-considerations.md#cloudpickle-and-multi-file-projects).

**Trace it.** This is a great workload to point at Jaeger — `map`
produces a fan of `pymonik.task.run` spans, each tagged with its
tile's row range. See [Observability](../guides/observability.md).

## Tuning

- **`NUM_RAYTRACING_TASKS`** controls fan-out. Too few and individual
  tasks dominate wall time; too many and submission overhead does.
  For a 600×400 image, 16-32 is a reasonable starting point.
- **`spill_threshold`** on the client controls when scene/camera get
  blobbed. Default 256 KiB is fine for medium scenes.
- **Scene complexity scales the per-pixel cost**. More spheres, more
  lights, deeper recursion = longer tasks. ArmoniK's per-task
  scheduling latency disappears into the noise once tasks take more
  than a second.
