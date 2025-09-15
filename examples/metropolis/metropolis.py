import numpy as np
import argparse
import vtk
from pymonik import Pymonik, task


def pdf(x):
    """
    Target distribution to generate samples
    """
    return np.exp(-10 * (x[0] ** 2 - x[1]) ** 2 - (x[1] - 0.25) ** 4)


def randn(mu, sigma):
    """
    Simple uniform random number generator
    """
    while True:
        U1 = np.random.uniform(-1, 1)
        U2 = np.random.uniform(-1, 1)
        W = U1**2 + U2**2
        if W < 1 and W > 0:
            break
    mult = np.sqrt(-2 * np.log(W) / W)
    return np.array([mu + sigma * U1 * mult, mu + sigma * U2 * mult])


def generate_contour_vtk(imax, jmax):
    """
    Generate vtk file with target distribution plot
    """
    pName = "target_distribution.vtk"

    x_coords = vtk.vtkDoubleArray()
    for i in range(imax):
        x_coords.InsertNextValue(-2.0 + (4.0 * i) / (imax - 1))

    y_coords = vtk.vtkDoubleArray()
    for j in range(jmax):
        y_coords.InsertNextValue(-1.0 + (3.0 * j) / (jmax - 1))

    z_coords = vtk.vtkDoubleArray()
    z_coords.InsertNextValue(0.0)

    grid = vtk.vtkRectilinearGrid()
    grid.SetDimensions(imax, jmax, 1)
    grid.SetXCoordinates(x_coords)
    grid.SetYCoordinates(y_coords)
    grid.SetZCoordinates(z_coords)

    target = vtk.vtkDoubleArray()
    target.SetName("target")
    x = np.empty(2)
    for j in range(jmax):
        for i in range(imax):
            x[0] = -2.0 + (4.0 * i) / (imax - 1)
            x[1] = -1.0 + (3.0 * j) / (jmax - 1)
            target.InsertNextValue(pdf(x))
    grid.GetPointData().SetScalars(target)

    writer = vtk.vtkRectilinearGridWriter()
    writer.SetFileName(pName)
    writer.SetInputData(grid)
    writer.SetFileTypeToASCII()
    writer.Write()

    print(f"Contour data written to {pName}")


@task
def run_metropolis_trial(x_init, imax, gamma, burn_in=0.1):
    """
    Runs a single Metropolis-Hastings chain of imax samples, starting from x_init.
    Returns the list of accepted sample points as (x, y) tuples, with the first
    burn_in fraction of points dropped since the chain hasn't converged yet.

    Executed as an independent ArmoniK task: each trial is its own chain,
    so trials can run in parallel rather than continuing one another.
    """
    x = np.array(x_init)
    points = []

    for _ in range(imax):
        pix = pdf(x)
        w = randn(0.0, gamma)

        y = x + w

        piy = pdf(y)

        alpha = min(1.0, piy / pix)

        U = np.random.rand()

        if U < alpha:
            x = y

        points.append((x[0], x[1]))

    n_burn_in = int(imax * burn_in)
    return points[n_burn_in:]


def write_trial_vtk(trial_index, points, glyph_size=0.02):
    """
    Writes the sample points of a single trial to a vtk file as 2D circle
    glyphs, so the point cloud is already visible in ParaView on open
    without needing to apply a Glyph filter by hand.
    """
    pName = f"metrop{trial_index}.vtk"

    vtk_points = vtk.vtkPoints()
    for px, py in points:
        vtk_points.InsertNextPoint(px, py, 0.0)

    point_data = vtk.vtkPolyData()
    point_data.SetPoints(vtk_points)

    glyph_source = vtk.vtkGlyphSource2D()
    glyph_source.SetGlyphTypeToCircle()
    glyph_source.SetScale(glyph_size)
    glyph_source.SetFilled(True)

    glyph = vtk.vtkGlyph3D()
    glyph.SetSourceConnection(glyph_source.GetOutputPort())
    glyph.SetInputData(point_data)
    glyph.SetScaleModeToDataScalingOff()
    glyph.Update()

    writer = vtk.vtkPolyDataWriter()
    writer.SetFileName(pName)
    writer.SetInputData(glyph.GetOutput())
    writer.SetFileTypeToASCII()
    writer.Write()


def main():
    """
    Runs Metropolis-Hastings algorithm to generate samples of the target distribution.

    Each trial is submitted as an independent ArmoniK task via Pymonik, since
    each trial is its own Markov chain starting from the same initial point.
    """
    parser = argparse.ArgumentParser(
        description="Run Metropolis sampling and generate contour VTK."
    )
    parser.add_argument("trials", type=int, help="Number of trials")
    parser.add_argument(
        "sizeSample", type=int, help="Number of samples to generate per trial"
    )
    parser.add_argument("gamma", type=float, help="Gamma value for the random walk")
    parser.add_argument(
        "--burn_in",
        type=float,
        default=0.1,
        help="Fraction of samples to discard as burn-in (default: 0.1)",
    )

    args = parser.parse_args()

    imax = args.sizeSample
    gamma = args.gamma
    trials = args.trials
    burn_in = args.burn_in

    print(f"Running with {imax} samples and gamma = {gamma}")

    # Initial value, shared as the starting point of every independent trial
    x_init = (1.5, -0.8)

    with Pymonik(environment={"pip": ["numpy"]}, endpoint="localhost:5001"):
        print(f"Submitting {trials} Metropolis trial tasks to Pymonik...")
        trial_args = [(x_init, imax, gamma, burn_in) for _ in range(trials)]
        results_handle = run_metropolis_trial.map_invoke(trial_args)

        print("Waiting for trials to complete...")
        results_handle.wait()

        for j in range(trials):
            points = results_handle[j].get()
            write_trial_vtk(j, points)

    print("Sampling done")

    print("Generate target distribution vtk")
    generate_contour_vtk(200, 200)


if __name__ == "__main__":
    main()
