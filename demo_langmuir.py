# Imports important python 3 behaviour to ensure correct operation and
# performance in python 2
from __future__ import print_function, division
import sys
if sys.version_info.major == 2:
    from itertools import izip as zip
    range = xrange

from dolfin import *
from punc import *
from numpy import pi
import numpy as np
from matplotlib import pyplot as plt

#==============================================================================
# INITIALIZING FENICS
#------------------------------------------------------------------------------

nDims = 2                           # Number of dimensions
Ld = 6.28*np.ones(nDims)            # Length of domain
Nr = 32*np.ones(nDims,dtype=int)    # Number of 'rectangles' in mesh

# mesh = RectangleMesh(Point(0,0),Point(Ld),*Nr)
mesh = Mesh("mesh/nonuniform.xml")
V = FunctionSpace(mesh, 'CG', 1, constrained_domain=PeriodicBoundary(Ld))
W = VectorFunctionSpace(mesh, 'CG', 1, constrained_domain=PeriodicBoundary(Ld))

#==============================================================================
# INITIALIZING POPULATION
#------------------------------------------------------------------------------

pop = Population(mesh)
distr = Distributor(V, Ld)
poisson = PoissonSolver(V,remove_null_space=True)

# A = 0.5
# mode = 1
# pdfMax = 1+A
# pdf = [lambda x, A=A, mode=mode, Ld=Ld: 1+A*np.sin(mode*2*np.pi*x[0]/Ld[0]),
#        lambda x: 1]
#
# init = Initialize(pop, pdf, Ld, [0,0], [0, 0], 16, pdfMax)
# init.initial_conditions()

initLangmuir(pop, Ld, 0., [0.,0.], 0.5, 1., 16)

dt = 0.251327
N = 30

KE = np.zeros(N-1)
PE = np.zeros(N-1)
KE0 = kineticEnergy(pop)

for n in range(1,N):
    print("Computing timestep %d/%d"%(n,N-1))
    rho = distr.distr(pop)
    rho = distr.charge_density(rho)
    phi = poisson.solve(rho)
    E = electric_field(phi)
    PE[n-1] = potentialEnergy(pop, phi)
    KE[n-1] = accel(pop,E,(1-0.5*(n==1))*dt)
    movePeriodic(pop,Ld,dt)
    pop.relocate()

KE[0] = KE0

plt.plot(KE,label="Kinetic Energy")
plt.plot(PE,label="Potential Energy")
plt.plot(KE+PE,label="Total Energy")
plt.legend(loc='lower right')
plt.grid()
plt.xlabel("Timestep")
plt.ylabel("Normalized Energy")
plt.show()

plot(rho)
plot(phi)

ux = Constant((1,0))
Ex = project(inner(E,ux),V)
plot(Ex)
interactive()
