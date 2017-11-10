from __future__ import print_function, division
import sys
if sys.version_info.major == 2:
    from itertools import izip as zip
    range = xrange

import dolfin as df
import numpy as np
import matplotlib.pyplot as plt
from punc import *
import os

# Simulation parameters
N        = 3000                   # Total simulation time
dt       = 0.15                  # Time step
npc      = 4

# Get the mesh
mesh, bnd = load_mesh("../mesh/2D/langmuir_probe_circle_in_square")
ext_bnd_id, int_bnd_ids = get_mesh_ids(bnd)

ext_bnd = ExteriorBoundaries(bnd, ext_bnd_id)

V = df.FunctionSpace(mesh, 'CG', 1)

bc = df.DirichletBC(V, df.Constant(0.0), bnd, ext_bnd_id)

# objects = [Object(V, int_bnd_id, bnd)]
objects = [Object(V, i, bnd) for i in int_bnd_ids]

ds = df.Measure("ds", domain=mesh, subdomain_data=bnd)
normal = df.FacetNormal(mesh)

# Get the solver
poisson = PoissonSolver(V, bc)

# The inverse of capacitance matrix
cap_factor = 1.5
inv_cap_matrix = capacitance_matrix(V, poisson, objects, bnd, ext_bnd_id)
inv_cap_matrix /= cap_factor

# Probe radius in mesh and debye lengths
Rp = 1. #0.000145 # m
Rpd = 1. # debye lengths
debye = Rp/Rpd
vthe = debye
vthi = debye/np.sqrt(1836.)

Vnorm = debye**2
Inorm = vthe*np.sqrt(2*np.pi)

# Initialize particle positions and velocities, and populate the domain
pop = Population(mesh, bnd, normalization='particle scaling')

if os.path.isfile('stop'):
    os.remove('stop')
    pop.init_new_specie('electron', ext_bnd, v_thermal=vthe, num_per_cell=npc, empty=True)
    pop.init_new_specie('proton',   ext_bnd, v_thermal=vthi, num_per_cell=npc, empty=True)
    pop.load_file('population.dat')
    f = open('state.dat','r')
    nstart = int(f.readline()) + 1
    objects[0].charge = float(f.readline())
    f.close()
    hist_file = open('history.dat', 'a')
else:
    nstart = 0
    pop.init_new_specie('electron', ext_bnd, v_thermal=vthe, num_per_cell=npc)
    pop.init_new_specie('proton',   ext_bnd, v_thermal=vthi, num_per_cell=npc)
    hist_file = open('history.dat', 'w')

# boltzmann = 1.38064852e-23 # J/K
# pfreq =
# denorm = pop.species.get_denorm(pfreq, debye, debye)
# Vnorm = denorm['V']
# Inorm = denorm['I']

dv_inv = voronoi_volume_approx(V)

KE  = np.zeros(N-1)
PE  = np.zeros(N-1)

current_collected = -1.556*Inorm

num_e = pop.num_of_negatives()
num_i = pop.num_of_positives()
potential = 0
current_measured = 0

timer = TaskTimer(N-1,'compact')

for n in range(nstart, N):

    # We are now at timestep n
    # Velocities and currents are at timestep n-0.5 (or 0 if n==0)

    timer.task("Distribute charge")
    rho = distribute(V, pop, dv_inv)

    timer.task("Calculate object potential")
    reset_objects(objects)
    phi     = poisson.solve(rho, objects)
    E       = electric_field(phi)
    obj_flux = df.inner(E, -1*normal)*ds(0)
    image_charge = df.assemble(obj_flux)
    object_potential = (objects[0].charge-image_charge)*inv_cap_matrix[0,0]
    objects[0].set_potential(df.Constant(object_potential))
    potential = objects[0]._potential/Vnorm # at n

    timer.task("Solving potential")
    phi     = poisson.solve(rho, objects)

    timer.task("Solving E-field")
    E  = electric_field(phi)
    PE = potential_energy(pop, phi)

    timer.task("Move particles")
    old_charge = objects[0].charge # Charge at n
    KE = accel(pop, E, (1-0.5*(n==1))*dt) # Advancing velocities to n+0.5
    if n==0: KE = kinetic_energy(pop)
    move(pop, dt) # Advancing position to n+1

    timer.task("Updating particles")
    pop.update(objects)

    timer.task("Write history")
    hist_file.write("%d\t%d\t%d\t%f\t%f\t%f\t%f\n"%(n, num_e, num_i, KE, PE, potential, current_measured))
    hist_file.flush()

    timer.task("Impose current")
    current_measured = ((objects[0].charge-old_charge)/dt)/Inorm # at n+0.5
    objects[0].add_charge(-current_collected*dt)

    timer.task("Inject particles")
    inject(pop, ext_bnd, dt)

    timer.task("Count particles")
    num_i = pop.num_of_positives()
    num_e = pop.num_of_negatives()

    if os.path.isfile('stop'):
        pop.save_file('population.dat')
        f = open('state.dat','w')
        f.write("%d\n%f"%(n,objects[0].charge))
        f.close()
        break

    timer.end()

timer.summary()
hist_file.close()

df.File('phi.pvd') << phi
df.File('rho.pvd') << rho
df.File('E.pvd') << E
