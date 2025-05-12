import math
import os
from pymonik import Pymonik, task
from PIL import Image 

# --- Helper Classes for Raytracing ---

class Vec3:
    """A simple 3D vector class with basic operations."""
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __add__(self, other):
        return Vec3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other):
        return Vec3(self.x - other.x, self.y - other.y, self.z - other.z)

    def __mul__(self, other):
        if isinstance(other, Vec3):
            return Vec3(self.x * other.x, self.y * other.y, self.z * other.z)
        elif isinstance(other, (int, float)):
            return Vec3(self.x * other, self.y * other, self.z * other)
        else:
            return NotImplemented

    def __rmul__(self, other):
        if isinstance(other, (int, float)):
            return Vec3(self.x * other, self.y * other, self.z * other)
        else:
            return NotImplemented

    def dot(self, other):
        return self.x * other.x + self.y * other.y + self.z * other.z

    def cross(self, other):
        return Vec3(
            self.y * other.z - self.z * other.y,
            self.z * other.x - self.x * other.z,
            self.x * other.y - self.y * other.x
        )

    def length_squared(self):
        return self.x*self.x + self.y*self.y + self.z*self.z

    def length(self):
        return math.sqrt(self.length_squared())

    def normalize(self):
        l = self.length()
        if l == 0: return Vec3(0,0,0)
        return Vec3(self.x / l, self.y / l, self.z / l)

    def to_tuple(self): # Still useful for other purposes, like debugging or if needed elsewhere
        return (self.x, self.y, self.z)

    def to_color(self):
        r = int(max(0, min(255, self.x * 255.999)))
        g = int(max(0, min(255, self.y * 255.999)))
        b = int(max(0, min(255, self.z * 255.999)))
        return (r, g, b)

    @staticmethod
    def from_tuple(t): # Still potentially useful
        return Vec3(t[0], t[1], t[2])


class Ray:
    def __init__(self, origin, direction):
        self.origin = origin
        self.direction = direction.normalize()

    def point_at_parameter(self, t):
        return self.origin + self.direction * t

class Material:
    def __init__(self, color, ambient=0.1, diffuse=0.9, specular=0.3, shininess=32):
        self.color = color
        self.ambient = ambient
        self.diffuse = diffuse
        self.specular = specular
        self.shininess = shininess


class Sphere:
    def __init__(self, center, radius, material):
        self.center = center
        self.radius = float(radius)
        self.material = material

    def intersect(self, ray):
        oc = ray.origin - self.center
        a = ray.direction.dot(ray.direction)
        b = 2.0 * oc.dot(ray.direction)
        c = oc.dot(oc) - self.radius * self.radius
        discriminant = b*b - 4*a*c
        if discriminant < 0:
            return None
        else:
            if abs(a) < 1e-6:
                return None
            sqrt_discriminant = math.sqrt(discriminant)
            t1 = (-b - sqrt_discriminant) / (2.0*a)
            t2 = (-b + sqrt_discriminant) / (2.0*a)
            epsilon = 0.001
            valid_ts = []
            if t1 > epsilon:
                valid_ts.append(t1)
            if t2 > epsilon:
                valid_ts.append(t2)
            if not valid_ts:
                return None
            return min(valid_ts)


class PointLight:
    def __init__(self, position, color, intensity=1.0):
        self.position = position
        self.color = color
        self.intensity = intensity


class Scene:
    def __init__(self, objects, lights, background_color=Vec3(0.1, 0.1, 0.3)):
        self.objects = objects
        self.lights = lights
        self.background_color = background_color


class Camera:
    def __init__(self, look_from, look_at, vup, vfov_degrees, aspect_ratio):
        self.origin = look_from
        self.vfov_rad = math.radians(vfov_degrees)
        self.aspect_ratio = aspect_ratio
        w = (look_from - look_at).normalize()
        u = vup.cross(w).normalize()
        v = w.cross(u).normalize()
        viewport_height = 2.0 * math.tan(self.vfov_rad / 2.0)
        viewport_width = self.aspect_ratio * viewport_height
        self.horizontal = u * viewport_width
        self.vertical = v * viewport_height
        viewport_center = self.origin - w
        self.lower_left_corner = viewport_center - (self.horizontal * 0.5) - (self.vertical * 0.5)

    def get_ray(self, u_norm, v_norm):
        direction = self.lower_left_corner + (self.horizontal * u_norm) + (self.vertical * v_norm) - self.origin
        return Ray(self.origin, direction.normalize())

# --- Core Raytracing Logic (executed by Pymonik tasks) ---

def trace_ray_for_pixel_color(ray, scene):
    min_t = float('inf')
    hit_object = None
    for obj in scene.objects:
        t = obj.intersect(ray)
        if t is not None and t < min_t:
            min_t = t
            hit_object = obj

    if hit_object:
        hit_point = ray.point_at_parameter(min_t)
        normal = (hit_point - hit_object.center).normalize()
        final_color = Vec3(0, 0, 0)
        ambient_light_contribution = hit_object.material.color * hit_object.material.ambient
        final_color += ambient_light_contribution

        for light in scene.lights:
            light_dir = (light.position - hit_point).normalize()
            diffuse_intensity_factor = max(0.0, normal.dot(light_dir))
            material_x_light_color = hit_object.material.color * light.color
            diffuse_color_contribution = material_x_light_color * diffuse_intensity_factor * hit_object.material.diffuse * light.intensity
            final_color += diffuse_color_contribution
        
        final_color.x = max(0.0, min(1.0, final_color.x))
        final_color.y = max(0.0, min(1.0, final_color.y))
        final_color.z = max(0.0, min(1.0, final_color.z))
        return final_color
    else:
        return scene.background_color

# --- Pymonik Task ---

@task
def render_tile_task(tile_y_start, tile_y_end, image_width, image_height, camera_obj, scene_obj):
    """
    Renders a horizontal strip (tile) of the image.
    Accepts scene and camera objects directly.
    """
    tile_pixel_data = []

    for y in range(tile_y_start, tile_y_end):
        for x in range(image_width):
            u_norm = (x + 0.5) / image_width
            v_norm = (image_height - 1 - y + 0.5) / image_height # Flipped y

            # Use the get_ray method from the camera object
            ray = camera_obj.get_ray(u_norm, v_norm)
            
            pixel_color_vec3 = trace_ray_for_pixel_color(ray, scene_obj)
            tile_pixel_data.append(pixel_color_vec3.to_color())
            
    return tile_y_start, tile_pixel_data


if __name__ == "__main__":
    img_width = 8000 
    img_height = 8000 

    material_red = Material(color=Vec3(1.0, 0.2, 0.2), ambient=0.1, diffuse=0.9)
    material_green = Material(color=Vec3(0.2, 1.0, 0.2), ambient=0.1, diffuse=0.8)
    material_blue = Material(color=Vec3(0.2, 0.2, 1.0), ambient=0.1, diffuse=0.7)
    material_grey_floor = Material(color=Vec3(0.5, 0.5, 0.5), ambient=0.2, diffuse=0.9)

    scene_objects = [
        Sphere(center=Vec3(0, 0, -1), radius=0.5, material=material_red),
        Sphere(center=Vec3(1.0, 0.2, -1.5), radius=0.7, material=material_green),
        Sphere(center=Vec3(-1.2, -0.1, -2.0), radius=0.4, material=material_blue),
        Sphere(center=Vec3(0, -100.5, -1), radius=100, material=material_grey_floor)
    ]
    scene_lights = [
        PointLight(position=Vec3(-2, 2, 1), color=Vec3(1.0, 1.0, 1.0), intensity=0.8),
        PointLight(position=Vec3(2, 1, 0), color=Vec3(0.8, 0.8, 1.0), intensity=0.6)
    ]
    scene_background = Vec3(0.7, 0.8, 1.0)
    
    main_scene = Scene(scene_objects, scene_lights, scene_background)

    look_from = Vec3(0, 0.5, 1.5)
    look_at = Vec3(0, 0, -1)
    vup = Vec3(0, 1, 0)
    vfov = 60.0
    aspect_ratio = img_width / img_height
    main_camera = Camera(look_from, look_at, vup, vfov, aspect_ratio)

    with Pymonik(endpoint="localhost:5001"):
        print("Successfully connected to Pymonik.")
        
        num_tasks = int(os.getenv("NUM_RAYTRACING_TASKS", "200")) 
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
                (y_start, y_end, img_width, img_height, main_camera, main_scene)
            )
        
        if not task_args_list:
            print("Error: No tasks generated. Check image dimensions and num_tasks.")
            if __name__ == '__main__':
                exit()

        print(f"Submitting {len(task_args_list)} raytracing tasks to Pymonik...")
        results_handle = render_tile_task.map_invoke(task_args_list)

        print("Waiting for tasks to complete...")
        results_handle.wait()
        print("All tasks completed. Fetching results...")

        final_image = Image.new("RGB", (img_width, img_height))
        rendered_tiles_data = {}
        for task_idx in range(len(task_args_list)):
            try:
                tile_y_start, tile_pixel_data = results_handle[task_idx].get()
                rendered_tiles_data[tile_y_start] = tile_pixel_data
                expected_rows_for_tile = task_args_list[task_idx][1] - task_args_list[task_idx][0]
                print(f"  Retrieved tile starting at row {tile_y_start} ({len(tile_pixel_data)} pixels for {expected_rows_for_tile} rows)")
            except Exception as e:
                print(f"  Error retrieving result for task {task_idx} (expected y_start {task_args_list[task_idx][0]}): {e}")
        
        print("Assembling final image...")
        default_missing_tile_color = (255, 0, 255)
        flat_pixel_list = [ default_missing_tile_color ] * (img_width * img_height)

        for original_task_args in task_args_list:
            y_start_key = original_task_args[0]
            expected_y_end = original_task_args[1]

            if y_start_key in rendered_tiles_data:
                tile_pixels = rendered_tiles_data[y_start_key]
                current_pixel_idx_in_tile = 0
                for y_offset in range(expected_y_end - y_start_key):
                    current_y = y_start_key + y_offset
                    if current_y >= img_height: continue
                    for x in range(img_width):
                        if current_pixel_idx_in_tile < len(tile_pixels):
                            flat_idx = current_y * img_width + x
                            if flat_idx < len(flat_pixel_list):
                                flat_pixel_list[flat_idx] = tile_pixels[current_pixel_idx_in_tile]
                            else:
                                print(f"Error: flat_idx {flat_idx} out of bounds for flat_pixel_list (len {len(flat_pixel_list)})")
                        else:
                            print(f"Warning: Missing pixel data in tile starting {y_start_key} at relative pixel {current_pixel_idx_in_tile} (x:{x}, y_offset:{y_offset})")
                        current_pixel_idx_in_tile +=1
            else:
                print(f"Warning: Missing data for entire tile starting at row {y_start_key}. Will be filled with default color.")

        final_image.putdata(flat_pixel_list)
        print(f"Final image assembled. Total pixels processed: {img_width * img_height}")

        output_filename = "pymonik_raytraced_image.png"
        try:
            final_image.save(output_filename)
            print(f"Image saved as {output_filename}")
        except Exception as e:
            print(f"Error saving or showing image: {e}")

    print("Raytracing finished.")