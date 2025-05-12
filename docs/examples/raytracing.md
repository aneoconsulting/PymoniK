# Distributed Raytracing in Python

Raytracing is a technique for generating realistic images by tracing the path of light as pixels in an image plane and simulating its effects with virtual objects. While capable of producing stunning visuals, raytracing is computationally intensive, as each pixel's color often requires complex calculations and many rays to be traced, especially for effects like reflections, refractions, and soft shadows. This makes it an excellent candidate for distributed computing.

PymoniK allows us to distribute the raytracing workload across multiple workers in an ArmoniK cluster, significantly speeding up the rendering process. We can divide the image into smaller sections (tiles) and assign each tile to a PymoniK task for parallel processing.

## Core Concept: Tiled Rendering

The basic idea is to break down the image rendering into smaller, independent tasks. Each task will be responsible for rendering a horizontal strip (or tile) of the final image.

1.  **Scene Definition**: We define a 3D scene containing objects (like spheres), light sources, and a camera.
2.  **Task Distribution**: The main client script divides the image into a number of horizontal tiles.
3.  **PymoniK Task**: A PymoniK task, `render_tile_task`, is defined. Each instance of this task receives:
    * The y-coordinates defining the start and end row of the tile it needs to render.
    * The overall image dimensions.
    * The `Camera` object.
    * The `Scene` object (containing all objects and lights).
4.  **Pixel Calculation**: Within each task, for every pixel in its assigned tile:
    * A ray is generated from the camera through the pixel.
    * This ray is traced into the scene to find the closest intersecting object.
    * The color of the pixel is determined based on the object's material, lighting, and other effects.
5.  **Result Aggregation**: The client script collects the pixel data (colors) for each rendered tile from the completed PymoniK tasks.
6.  **Image Assembly**: Finally, the client assembles these tiles into the complete image.

## Prerequisites

Ensure you have the necessary Python packages installed:

```bash
uv add pymonik Pillow
```

* `pymonik`: For interacting with the ArmoniK cluster.
* `Pillow`: For image manipulation (creating and saving the final image) on the client side.

## PymoniK Implementation

Let's look at the key parts of the Python script. The full script also includes helper classes for 3D vectors (`Vec3`), rays (`Ray`), materials (`Material`), spheres (`Sphere`), lights (`PointLight`), the scene (`Scene`), and the camera (`Camera`). PymoniK will handle the serialization of these custom objects automatically when they are passed as arguments to tasks.

### The Raytracing Task

The core of the distributed computation is the `render_tile_task` function, decorated with `@task` to make it a PymoniK task.

```py
import math
from pymonik import task
# Assuming Vec3, Ray, Camera, Scene, trace_ray_for_pixel_color etc. are defined elsewhere

@task
def render_tile_task(tile_y_start, tile_y_end, image_width, image_height, camera_obj, scene_obj): #(1)
    """
    Renders a horizontal strip (tile) of the image.
    Accepts scene and camera objects directly.
    """
    tile_pixel_data = [] # List of (r,g,b) tuples for this tile

    for y in range(tile_y_start, tile_y_end): #(2)
        # print(f"Worker rendering row {y}/{image_height}") # Optional: progress within worker
        for x in range(image_width):
            # u, v are normalized screen coordinates (0 to 1)
            # Add 0.5 for sampling at the center of the pixel
            u_norm = (x + 0.5) / image_width  
            v_norm = (image_height - 1 - y + 0.5) / image_height # Flipped y for typical image coords
            
            # Use the get_ray method from the camera object
            # PymoniK handles sending the camera_obj to the worker
            ray = camera_obj.get_ray(u_norm, v_norm) #(3)
            
            # trace_ray_for_pixel_color uses scene_obj (also sent by PymoniK)
            pixel_color_vec3 = trace_ray_for_pixel_color(ray, scene_obj) #(4)
            tile_pixel_data.append(pixel_color_vec3.to_color())
            
    # Return the starting row index and the pixel data for this tile
    return tile_y_start, tile_pixel_data #(5)
```

1. It receives `camera_obj` and `scene_obj` directly. PymoniK takes care of serializing these objects and sending them to the worker where the task executes.
2. It iterates over its assigned rows (`tile_y_start` to `tile_y_end`) and columns (`image_width`).
3. For each pixel, it uses `camera_obj.get_ray()` to generate a ray.
4. `trace_ray_for_pixel_color(ray, scene_obj)` performs the actual raytracing logic for that single ray against the scene.
5. It returns the starting y-coordinate of the tile and a list of pixel colors for that tile.

### Main Client Logic

The client-side script sets up the scene, camera, connects to PymoniK, divides the work, submits tasks, and then assembles the final image.

```python
# --- Main Application Logic (Client Side) ---
# Assuming imports for os, Pymonik, Image, math, and helper classes like Vec3, Scene, Camera etc.

if __name__ == "__main__":
    # Image dimensions
    img_width = 600 
    img_height = 400

    # Scene setup (materials, objects, lights)
    # ... (material_red, material_green, etc.)
    # ... (scene_objects list of Sphere instances)
    # ... (scene_lights list of PointLight instances)
    # ... (scene_background Vec3)
    # main_scene = Scene(scene_objects, scene_lights, scene_background)

    # Camera setup
    # ... (look_from, look_at, vup, vfov, aspect_ratio)
    # main_camera = Camera(look_from, look_at, vup, vfov, aspect_ratio)


    with Pymonik(endpoint="localhost:5001"):
        print("Successfully connected to Pymonik.")
        
        # Divide work: each task renders a few rows
        num_tasks = int(os.getenv("NUM_RAYTRACING_TASKS", "10")) 
        num_tasks = max(1, min(num_tasks, img_height)) 
        rows_per_task = math.ceil(img_height / num_tasks)
        
        task_args_list = []
        for i in range(num_tasks):
            y_start = i * rows_per_task
            y_end = min((i + 1) * rows_per_task, img_height)
            if y_start >= y_end: 
                continue
            # Pass main_camera and main_scene objects directly
            task_args_list.append(
                (y_start, y_end, img_width, img_height, main_camera, main_scene) # main_camera and main_scene are actual objects
            )
        
        if not task_args_list:
            print("Error: No tasks generated. Check image dimensions and num_tasks.")
            exit()

        print(f"Submitting {len(task_args_list)} raytracing tasks to Pymonik...")
        # map_invoke submits all tasks in parallel
        results_handle = render_tile_task.map_invoke(task_args_list)

        print("Waiting for tasks to complete...")
        results_handle.wait() # Wait for all tasks to finish
        print("All tasks completed. Fetching results...")

        # Prepare to assemble the image
        final_image = Image.new("RGB", (img_width, img_height))
        
        rendered_tiles_data = {} 
        for task_idx in range(len(task_args_list)):
            try:
                # results_handle is a MultiResultHandle, access individual results by index
                tile_y_start, tile_pixel_data = results_handle[task_idx].get()
                rendered_tiles_data[tile_y_start] = tile_pixel_data
                # ... (logging)
            except Exception as e:
                # ... (error handling)
        
        print("Assembling final image...")
        # ... (Logic to iterate through rendered_tiles_data and put pixels into final_image)
        # Example:
        # flat_pixel_list = [ (255,0,255) ] * (img_width * img_height) # Default for missing
        # for y_start_key, tile_pixels in rendered_tiles_data.items():
        #     # ... (detailed logic to place tile_pixels into flat_pixel_list)
        # final_image.putdata(flat_pixel_list)


        output_filename = "pymonik_raytraced_image.png"
        try:
            final_image.save(output_filename)
            print(f"Image saved as {output_filename}")
        except Exception as e:
            print(f"Error saving or showing image: {e}")

    print("Raytracing finished.")
```


- **Objects as Arguments**: The `main_camera` and `main_scene` objects are passed directly when building `task_args_list`. PymoniK handles their distribution.
- **`map_invoke`**: This PymoniK method is used to submit multiple instances of `render_tile_task` with different arguments (different tiles) in parallel. It returns a `MultiResultHandle`.
- **Result Handling**: `results_handle.wait()` blocks until all tasks are complete. Then, individual results are fetched using `results_handle[task_idx].get()`.
- **Image Assembly**: The `Pillow` library is used to create a new image and populate it with the pixel data returned by the tasks.

!!! tip "Full Code"
    The snippets above are excerpts. You would need the full definitions for classes like `Vec3`, `Sphere`, `Camera`, `Scene`, and the `trace_ray_for_pixel_color` function for a complete runnable example. Please check `examples/raytracing` for that

## Running the Example

1.  Save the complete Python script (including helper classes and the PymoniK logic shown above) as a `.py` file (e.g., `distributed_raytracer.py`).
2.  Ensure your ArmoniK cluster is running and accessible.
3.  Set the environment variable `ARMONIK_ENDPOINT` if it's not the default `localhost:5001`.
    ```bash
    export ARMONIK_ENDPOINT="your_armonik_controller_ip:your_port"
    ```
4.  You can also control the number of tasks (and thus, tiles) using the `NUM_RAYTRACING_TASKS` environment variable.
    ```bash
    export NUM_RAYTRACING_TASKS=20 # Example: divide into 20 tiles
    ```
5.  Run the script:
    ```bash
    python distributed_raytracer.py
    ```

## Expected Output

After the script completes, you should find an image file named `pymonik_raytraced_image.png` (or similar, based on your output filename) in the same directory. This image will be the result of the distributed raytracing computation.

The console output will show connection messages, task submission progress, and final assembly messages.
