from pymonik import Pymonik, task, materialize, Materialize
import os
from pathlib import Path

# === EXAMPLE 1: Basic file materialization ===

@task
def process_config_file(config_mat: Materialize):
    """Process a configuration file that was materialized in the worker."""
    config_path = Path(config_mat.worker_path)
    
    if not config_path.exists():
        return f"Error: Config file not found at {config_path}"
    
    # Read and process the config file
    with open(config_path, 'r') as f:
        config_content = f.read()
    
    return f"Processed config from {config_path}: {len(config_content)} characters"


# === EXAMPLE 2: Directory materialization ===

@task  
def process_dataset(dataset_mat: Materialize):
    """Process a dataset directory that was materialized in the worker."""
    dataset_path = Path(dataset_mat.worker_path)
    
    if not dataset_path.exists():
        return f"Error: Dataset directory not found at {dataset_path}"
    
    # Count files in the dataset
    file_count = sum(1 for _ in dataset_path.rglob('*') if _.is_file())
    total_size = sum(f.stat().st_size for f in dataset_path.rglob('*') if f.is_file())
    
    return {
        "dataset_path": str(dataset_path),
        "file_count": file_count,
        "total_size_bytes": total_size,
        "hash": dataset_mat.content_hash
    }


# === EXAMPLE 3: Multiple materializations ===

@task
def compare_datasets(dataset1_mat: Materialize, dataset2_mat: Materialize):
    """Compare two materialized datasets."""
    
    results = {}
    
    for name, mat in [("dataset1", dataset1_mat), ("dataset2", dataset2_mat)]:
        dataset_path = Path(mat.worker_path)
        
        if dataset_path.exists():
            file_count = sum(1 for _ in dataset_path.rglob('*') if _.is_file())
            results[name] = {
                "path": str(dataset_path),
                "files": file_count,
                "hash": mat.content_hash,
                "exists": True
            }
        else:
            results[name] = {"exists": False, "path": str(dataset_path)}
    
    return results


# === EXAMPLE 4: Using materialization with context ===

@task(require_context=True)
def advanced_file_processing(ctx, data_mat: Materialize, output_dir: str):
    """Advanced processing with access to context for logging."""
    
    ctx.logger.info(f"Processing materialized data: {data_mat.source_path}")
    ctx.logger.info(f"Worker path: {data_mat.worker_path}")
    ctx.logger.info(f"Content hash: {data_mat.content_hash}")
    
    data_path = Path(data_mat.worker_path)
    
    if not data_path.exists():
        ctx.logger.error(f"Data not found at {data_path}")
        return {"success": False, "error": "Data not materialized"}
    
    # Process the data (example: copy files to output directory)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    processed_files = []
    if data_path.is_file():
        # Single file
        import shutil
        output_file = output_path / data_path.name
        shutil.copy2(data_path, output_file)
        processed_files.append(str(output_file))
        ctx.logger.info(f"Copied file to {output_file}")
    else:
        # Directory
        import shutil
        for file_path in data_path.rglob('*'):
            if file_path.is_file():
                rel_path = file_path.relative_to(data_path)
                output_file = output_path / rel_path
                output_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file_path, output_file)
                processed_files.append(str(output_file))
        ctx.logger.info(f"Copied {len(processed_files)} files to {output_path}")
    
    return {
        "success": True,
        "input_hash": data_mat.content_hash,
        "processed_files": processed_files,
        "output_directory": str(output_path)
    }


# === CLIENT USAGE EXAMPLES ===

if __name__ == "__main__":
    
    # Setup - create some test files/directories
    test_dir = Path("./test_materials")
    test_dir.mkdir(exist_ok=True)
    
    # Create a test config file
    config_file = test_dir / "config.txt"
    with open(config_file, "w") as f:
        f.write("debug=true\nmax_workers=10\ntimeout=302\n")
    
    # Create a test dataset directory
    dataset_dir = test_dir / "dataset"
    dataset_dir.mkdir(exist_ok=True)
    for i in range(5):
        data_file = dataset_dir / f"data_{i}.txt"
        with open(data_file, "w") as f:
            f.write(f"Sample data file {i}\n" * (i + 1))
    
    # Create a subdirectory in dataset
    subdir = dataset_dir / "subdir"
    subdir.mkdir(exist_ok=True)
    (subdir / "nested_file.txt").write_text("Nested content")
    
    with Pymonik(endpoint="localhost:5001") as pk:
        
        print("=== Creating Materialize objects ===")
        
        # Example 1: Materialize a single config file
        config_mat = materialize(config_file, "/tmp/worker_config.txt")
        print(f"Config file hash: {config_mat.content_hash}")
        
        # Upload the materialize object
        config_mat = pk.upload_materialize(config_mat, force_upload=True)
        print(f"Config uploaded with result_id: {config_mat.result_id}")
        
        # Example 2: Materialize a directory (will be zipped)
        dataset_mat = materialize(dataset_dir, "/tmp/worker_dataset")
        print(f"Dataset directory hash: {dataset_mat.content_hash}")
        
        # Upload the dataset
        dataset_mat = pk.upload_materialize(dataset_mat, force_upload=True)
        print(f"Dataset uploaded with result_id: {dataset_mat.result_id}")
        
        # Example 3: Create another dataset for comparison
        dataset2_dir = test_dir / "dataset2"
        dataset2_dir.mkdir(exist_ok=True)
        (dataset2_dir / "different_file.txt").write_text("Different content")
        
        dataset2_mat = materialize(dataset2_dir, "/tmp/worker_dataset2")
        dataset2_mat = pk.upload_materialize(dataset2_mat, force_upload=True)
        
        print("\n=== Running tasks with materialized content ===")
        
        # Test 1: Process config file
        result1 = process_config_file.invoke(config_mat).wait().get()
        print(f"Config processing result: {result1}")
        
        # Test 2: Process dataset
        result2 = process_dataset.invoke(dataset_mat).wait().get()
        print(f"Dataset processing result: {result2}")
        
        # Test 3: Compare datasets
        result3 = compare_datasets.invoke(dataset_mat, dataset2_mat).wait().get()
        print(f"Dataset comparison result: {result3}")
        
        # Test 4: Advanced processing with context
        result4 = advanced_file_processing.invoke(
            dataset_mat, "/tmp/worker_output"
        ).wait().get()
        print(f"Advanced processing result: {result4}")
        
        print("\n=== Testing deduplication ===")
        
        # Create the same config file again - should reuse existing upload
        config_file_2 = test_dir / "config_copy.txt"
        with open(config_file_2, "w") as f:
            f.write("debug=true\nmax_workers=10\ntimeout=300\n")  # Same content
        
        config_mat_2 = materialize(config_file_2, "/tmp/worker_config_2.txt")
        print(f"Second config hash: {config_mat_2.content_hash}")
        print(f"Hashes match: {config_mat.content_hash == config_mat_2.content_hash}")
        
        # This should reuse the existing upload
        config_mat_2 = pk.upload_materialize(config_mat_2)
        print(f"Second config result_id: {config_mat_2.result_id}")
        print(f"Result IDs match: {config_mat.result_id == config_mat_2.result_id}")
        
        print("\n=== All tests completed ===")
