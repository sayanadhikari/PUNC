# Copyright (C) 2017, Sigvald Marholm and Diako Darian
#
# This file is part of PUNC.
#
# PUNC is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# PUNC is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# PUNC.  If not, see <http://www.gnu.org/licenses/>.
#
# Loosely based on fenicstools/LagrangianParticles by Mikeal Mortensen and
# Miroslav Kuchta.

from __future__ import print_function, division
import sys
if sys.version_info.major == 2:
    from itertools import izip as zip
    range = xrange

import dolfin as df
import numpy as np
from mpi4py import MPI as pyMPI
from collections import defaultdict
from itertools import count
from punc.poisson import get_mesh_size
from punc.injector import create_mesh_pdf, Flux, maxwellian, random_domain_points, locate

comm = pyMPI.COMM_WORLD
__UINT32_MAX__ = np.iinfo('uint32').max
class Particle(object):
    __slots__ = ('x', 'v', 'q', 'm')
    def __init__(self, x, v, q, m):
        assert q!=0 and m!=0
        self.x = np.array(x)    # Position vector
        self.v = np.array(v)    # Velocity vector
        self.q = q              # Charge
        self.m = m              # Mass

    def send(self, dest):
        comm.Send(self.x, dest=dest)
        comm.Send(self.v, dest=dest)
        comm.Send(self.q, dest=dest)
        comm.Send(self.m, dest=dest)

    def recv(self, source):
        comm.Recv(self.x, source=source)
        comm.Recv(self.v, source=source)
        comm.Recv(self.q, source=source)
        comm.Recv(self.m, source=source)

class Specie(object):
    """
    A specie with q elementary charges and m electron masses is specified as
    follows:

        s = Specie((q,m))

    Alternatively, electrons and protons may be specified by an 'electron' or
    'proton' string instead of a tuple:

        s = Specie('electron')

    The following keyword arguments are accepted to change default behavior:

        v_drift
            Drift velocity of specie. Default: 0.

        v_thermal
            Thermal velocity of specie. Default: 0.
            Do not use along with temperature.

        temperature
            Temperature of specie. Default: 0.
            Do not use along with v_thermal.

        num_per_cell
            Number of particles per cell. Default: 16.

        num_total
            Number of particles in total.
            Overrides num_per_cell if specified.

    E.g. to specify electrons with thermal and drift velocities:

        s = Specie('electron', v_thermal=1, v_drift=[1,0])

    Note that the species have to be normalized before being useful. Species
    are typically put in a Species list and normalized before being used. See
    Species.
    """

    def __init__(self, specie, **kwargs):

        # Will be set during normalization
        self.charge = None
        self.mass = None
        self.v_thermal = None
        self.v_drift = None

        self.v_thermal_raw = 0
        self.temperature_raw = None
        self.v_drift_raw = 0

        self.num_total = None
        self.num_per_cell = 16

        if specie == 'electron':
            self.charge_raw = -1
            self.mass_raw = 1

        elif specie == 'proton':
            self.charge_raw = 1
            self.mass_raw = 1836.15267389

        else:
            assert isinstance(specie,tuple) and len(specie)==2 ,\
                "specie must be a valid keyword or a (charge,mass)-tuple"

            self.charge_raw = specie[0]
            self.mass_raw = specie[1]

        if 'num_per_cell' in kwargs:
            self.num_per_cell = kwargs['num_per_cell']

        if 'num_total' in kwargs:
            self.num_total = kwargs['num_total']

        if 'v_thermal' in kwargs:
            self.v_thermal_raw = kwargs['v_thermal']

        if 'v_drift' in kwargs:
            self.v_drift_raw = kwargs['v_drift']

        if 'temperature' in kwargs:
            self.temperature_raw = kwargs['temperature']

class Species(list):
    """
    Just a normal list of Specie objects except that the method append_specie()
    may be used to append species to the list and normalize them.
    append_specie() takes the same argumets as the Specie() constructor.

    Two normalization schemes are implemented as can be chosen using the
    'normalization' parameter in the constructor:

        'plasma params' (default, obsolete):
            The zeroth specie in the list (i.e. the first appended one) is
            normalized to have an angular plasma frequency of one and a thermal
            velocity of 1 (and hence also a Debye length of one). If the specie
            is cold the thermal velocity is 0 and the Debye length does not act
            as a characteristic length scale in the simulations.

        'particle scaling':
            The charge and mass of the particles are given statistical weights
            such that the plasma frequency of the zeroth species is normalized
            to 1. To allow changing the ratio of the geometry to the Debye
            length without making a new mesh, the Debye length is not
            normalized to any particular value. Instead, the thermal velocity
            must be specified relative to the sizes of the geometry in the mesh.
            The Debye length in this unit will be given by v_th=lambda_D*w_p.

        'none':
            The specified charge, mass, drift and thermal velocities are used
            as specified without further normalization.

    E.g. to create isothermal electrons and ions normalized such that the
    electron parameters are all one:

        species = Species(mesh)
        species.append_specie('electron', temperature=1) # reference
        species.append_specie('proton'  , temperature=1)

    """

    def __init__(self, mesh, normalization='plasma params'):
        self.volume = df.assemble(1*df.dx(mesh))
        self.num_cells = mesh.num_cells()

        assert normalization in ('plasma params', 'particle scaling', 'none')

        if normalization == 'plasma params':
            self.normalize = self.normalize_plasma_params

        if normalization == 'particle scaling':
            self.normalize = self.normalize_particle_scaling

        if normalization == 'none':
            self.normalize = self.normalize_none

    def append_specie(self, specie, **kwargs):
        self.append(Specie(specie, **kwargs))
        self.normalize(self[-1])

    def normalize_none(self, s):
        if s.num_total == None:
            s.num_total = s.num_per_cell * self.num_cells

        s.charge = s.charge_raw
        s.mass = s.mass_raw
        s.v_thermal = s.v_thermal_raw
        s.v_drift = s.v_drift_raw
        self.weight = 1

    def normalize_plasma_params(self, s):
        if s.num_total == None:
            s.num_total = s.num_per_cell * self.num_cells

        ref = self[0]
        w_pe = 1
        self.weight = (w_pe**2) \
               * (self.volume/ref.num_total) \
               * (ref.mass_raw/ref.charge_raw**2)

        s.charge = self.weight*s.charge_raw
        s.mass = self.weight*s.mass_raw

        if ref.temperature_raw != None:
            assert s.temperature_raw != None, \
                "Specify temperature for all or none species"

            ref.v_thermal = 1
            for s in self:
                s.v_thermal = ref.v_thermal*np.sqrt( \
                    (s.temperature_raw/ref.temperature_raw) * \
                    (ref.mass_raw/s.mass_raw) )
        elif s.v_thermal_raw == 0:
            s.v_thermal = 0
        else:
            s.v_thermal = s.v_thermal_raw/ref.v_thermal_raw

        if (isinstance(s.v_drift_raw, np.ndarray) and \
           all(i == 0 for i in s.v_drift_raw) ):
            s.v_drift = np.zeros((s.v_drift_raw.shape))
        elif isinstance(s.v_drift_raw, (float,int)) and s.v_drift_raw==0:
            s.v_drift = 0
        else:
            s.v_drift = s.v_drift_raw/ref.v_thermal_raw

    def normalize_particle_scaling(self, s):
        if s.num_total == None:
            s.num_total = s.num_per_cell * self.num_cells

        ref = self[0]
        w_pe = 1
        self.weight = (w_pe**2) \
               * (self.volume/ref.num_total) \
               * (ref.mass_raw/ref.charge_raw**2)

        s.charge = self.weight*s.charge_raw
        s.mass = self.weight*s.mass_raw

        assert s.temperature_raw == None, \
                "This normalization does not support providing temperatures"

        s.v_thermal = s.v_thermal_raw
        s.v_drift   = s.v_drift_raw

    def get_denorm(phys_pfreq, phys_debye, sim_debye):
        """
        Returns a dictionary of multiplicative factors which can be used to
        dimensionalize simulation units to SI units. The input is the physical
        angular plasma frequency [rad/s], the physical debye length [m] as well
        as how long a debye length is in the units of the mesh.
        """
        electron_mass = 9.10938188e-31 # kg
        elementary_charge = 1.60217646e-19 # C
        vacuum_permittivity = 8.854187817e-12 # F/m

        ref = self[0]
        ref_charge_SI = elementary_charge*ref.charge_raw
        ref_mass_SI = electron_mass*ref.mass_raw
        qm_ratio = (ref.charge/ref.mass)/(ref_charge_SI/ref_mass_SI)

        denorm = dict()
        denorm['t'] = 1./phys_pfreq
        denorm['x'] = phys_debye/sim_debye
        denorm['q'] = ref_charge_SI/ref.charge
        denorm['m'] = ref_mass_SI/ref.mass
        denorm['v'] = denorm['x']/denorm['t']
        denorm['rho'] = qm_ratio*vacuum_permittivity/(denorm['t']**2)
        denorm['phi'] = qm_ratio*(denorm['x']/denorm['t'])**2
        denorm['V'] = denorm['phi']
        denorm['I'] = (vacuum_permittivity/qm_ratio)*denorm['x']**3/denorm['t']**4
        return denorm

class Population(list):
    """
    Represents a population of particles. self[i] is a list of all Particle
    objects belonging to cell i in the DOLFIN mesh. Note that particles, when
    moved, do not automatically appear in the correct cell. Instead relocate()
    must be invoked to relocate the particles.
    """

    def __init__(self, mesh, boundaries, periodic=None, normalization='plasma params'):
        self.mesh = mesh
        self.Ld = get_mesh_size(mesh)
        self.periodic = periodic
       
        # Particle flux and plasma density     
        self.flux = []
        self.plasma_density = []
        self.N = []
        self.volume = df.assemble(1*df.dx(mesh))
 
        # Species
        self.species = Species(mesh, normalization)

        # Allocate a list of particles for each cell
        for cell in df.cells(self.mesh):
            self.append(list())

        # Create a list of sets of neighbors for each cell
        self.t_dim = self.mesh.topology().dim()
        self.g_dim = self.mesh.geometry().dim()

        self.mesh.init(0, self.t_dim)
        self.tree = self.mesh.bounding_box_tree()
        self.neighbors = list()
        for cell in df.cells(self.mesh):
            neigh = sum([vertex.entities(self.t_dim).tolist() for vertex in df.vertices(cell)], [])
            neigh = set(neigh) - set([cell.index()])
            self.neighbors.append(neigh)

        # Allocate some MPI stuff
        self.num_processes = comm.Get_size()
        self.myrank = comm.Get_rank()
        self.all_processes = list(range(self.num_processes))
        self.other_processes = list(range(self.num_processes))
        self.other_processes.remove(self.myrank)
        self.my_escaped_particles = np.zeros(1, dtype='I')
        self.tot_escaped_particles = np.zeros(self.num_processes, dtype='I')
        # Dummy particle for receiving/sending at [0, 0, ...]
        v_zero = np.zeros(self.g_dim)
        self.particle0 = Particle(v_zero,v_zero,1,1)

        self.init_localizer(boundaries)

    def init_localizer(self, boundaries):
        # self.facet_adjacents[cell_id][facet_number] is the id of the adjacent cell
        # self.facet_normals[cell_id][facet_number] is the normal vector to a facet
        # self.facet_mids[cell_id][facet_number] is the midpoint on a facet
        # facet_number is a number from 0 to t_dim
        # TBD: Now all facets are stored redundantly (for each cell)
        # Storage could be reduced, but would the performance hit be significant?

        self.mesh.init(self.t_dim-1, self.t_dim)
        self.facet_adjacents = []
        self.facet_normals = []
        self.facet_mids = []
        facets = list(df.facets(self.mesh))
        for cell in df.cells(self.mesh):
            facet_ids = cell.entities(self.t_dim-1)
            adjacents = []
            normals = []
            mids = []

            for facet_number, facet_id in enumerate(facet_ids):
                facet = facets[facet_id]

                adjacent = set(facet.entities(self.t_dim))-{cell.index()}
                adjacent = list(adjacent)
                if adjacent == []:
                    # Travelled out of bounds through the following boundary
                    # Minus indicates through boundary
                    adjacent = -int(boundaries.array()[facet_id])

                else:
                    adjacent = int(adjacent[0])

                assert isinstance(adjacent,int)


                # take normal from cell rather than from facet to make sure it is outwards-pointing
                normal = [cell.normal(facet_number, i) for i in range(self.t_dim)]

                mid = facet.midpoint()
                mid = np.array([mid.x(), mid.y(), mid.z()])
                mid = mid[:self.t_dim]

                adjacents.append(adjacent)
                normals.append(normal)
                mids.append(mid)


            self.facet_adjacents.append(adjacents)
            self.facet_normals.append(normals)
            self.facet_mids.append(mids)

    def init_new_specie(self, specie, exterior_bnd, **kwargs):
        """
        To initialize a new specie within a population use this function, e.g.
        to uniformly populate the domain with 16 (default) cold electrons and
        protons per cell:

            pop = Population(mesh)
            pop.init_new_specie('electron')
            pop.init_new_specie('proton')

        Here, the normalization is such that the electron plasma frequency and
        Debye lengths are set to one. The electron is used as a reference
        because that specie is initialized first.

        All species is represented as a Species object internally in the
        population and consequentially, the init_new_specie() method takes the
        same arguments as the append_specie() method in the Species class. See
        that method for information of how to tweak specie properties.

        In addition, init_new_specie() takes two additional keywords:

            pdf:
                A probability density function of how to distribute particles.

            pdf_max:
                An upper bound for the values in the pdf.

        E.g. to initialize cold langmuir oscillations (where the initial
        electron density is sinusoidal) in the x-direction in a unit length
        domain:

            pop = Population(mesh)
            pdf = lambda x: 1+0.1*np.sin(2*np.pi*x[0])
            pop.init_new_specie('electron', pdf=pdf, pdf_max=1.1)
            pop.init_new_specie('proton')

        """

        self.species.append_specie(specie, **kwargs)

        if 'pdf' in kwargs:
            pdf = kwargs['pdf']
        else:
            pdf = lambda x: 1

        if pdf != None:

            pdf = create_mesh_pdf(pdf, self.mesh)

            if 'pdf_max' in kwargs:
                pdf_max = kwargs['pdf_max']
            else:
                pdf_max = 1

        m = self.species[-1].mass
        q = self.species[-1].charge
        v_thermal = self.species[-1].v_thermal
        v_drift = self.species[-1].v_drift
        num_total = self.species[-1].num_total

        self.plasma_density.append(num_total / self.volume)
        self.flux.append(Flux(v_thermal, v_drift, exterior_bnd))
        self.N.append(self.flux[-1].flux_number(exterior_bnd))

        if not 'empty' in kwargs:
            xs = random_domain_points(pdf, pdf_max, num_total, self.mesh)
            vs = maxwellian(v_thermal, v_drift, xs.shape)
            self.add_particles(xs,vs,q,m)

    def add_particles_of_specie(self, specie, xs, vs=None):
        q = self.species[specie].charge
        m = self.species[specie].mass
        self.add_particles(xs, vs, q, m)

    def add_particles(self, xs, vs=None, qs=None, ms=None):
        """
        Adds particles to the population and locates them on their home
        processor. xs is a list/array of position vectors. vs, qs and ms may
        be lists/arrays of velocity vectors, charges, and masses,
        respectively, or they may be only a single velocity vector, mass
        and/or charge if all particles should have the same value.
        """

        if vs is None or qs is None or ms is None:
            assert isinstance(xs,list)
            if len(xs)==0:
                return
            assert isinstance(xs[0],Particle)
            ps = xs
            xs = [p.x for p in ps]
            vs = [p.v for p in ps]
            qs = [p.q for p in ps]
            ms = [p.m for p in ps]
            self.add_particles(xs, vs, qs, ms)
            return

        # Expand input to lists/arrays if necessary
        if len(np.array(vs).shape)==1: vs = np.tile(vs,(len(xs),1))
        if not isinstance(qs, (np.ndarray,list)): qs *= np.ones(len(xs))
        if not isinstance(ms, (np.ndarray,list)): ms *= np.ones(len(xs))

        # Keep track of which particles are located locally and globally
        my_found = np.zeros(len(xs), np.int)
        all_found = np.zeros(len(xs), np.int)

        for i, x, v, q, m in zip(count(), xs, vs, qs, ms):
            cell_id = self.locate(x)
            if cell_id >=0:
                self[cell_id].append(Particle(x, v, q, m))

    def locate(self, x):
        return locate(self.mesh, x)

    def relocate(self, p, cell_id):

        cell = df.Cell(self.mesh, cell_id)
        if cell.contains(df.Point(*p)):
            return cell_id
        else:
            x = p - np.array(self.facet_mids[cell_id])

            # The projection of x on each facet normal. Negative if behind facet.
            # If all negative particle is within cell
            proj = np.sum(x*self.facet_normals[cell_id], axis=1)
            projarg = np.argmax(proj)
            new_cell_id = self.facet_adjacents[cell_id][projarg]
            if new_cell_id>=0:
                return self.relocate(p, new_cell_id)
            else:
                return new_cell_id # crossed a boundary

    def update(self, objects = None):

        if objects == None: objects = []

        # TBD: Could possibly be placed elsewhere
        object_domains = [o._sub_domain for o in objects]
        object_ids = dict()
        for o,d in enumerate(object_domains):
            object_ids[d] = o

        # This times dt is an accurate measurement of collected current
        # collected_charge = np.zeros(len(objects))

        for cell_id, cell in enumerate(self):

            to_delete = []

            for particle_id, particle in enumerate(cell):

                new_cell_id = self.relocate(particle.x, cell_id)

                if new_cell_id != cell_id:

                    # Particle has moved out of cell.
                    # Mark it for deletion
                    to_delete.append(particle_id)

                    if new_cell_id < 0:
                        # Particle has crossed a boundary, either external
                        # or internal (into an object) and do not reappear
                        # in a new cell.

                        if -new_cell_id in object_ids:
                            # Particle entered object. Accumulate charge.
                            # collected_charge[object_ids[-new_cell_id]] += particle.q
                            obj = objects[object_ids[-new_cell_id]]
                            obj.charge += particle.q
                    else:
                        # Particle has moved to another cell
                        self[new_cell_id].append(particle)

            # Delete particles in reverse order to avoid altering the id
            # of particles yet to be deleted.
            for particle_id in reversed(to_delete):

                if particle_id==len(cell)-1:
                    # Particle is the last element
                    cell.pop()
                else:
                    # Delete by replacing it by the last element in the list.
                    # More efficient then shifting the whole list.
                    cell[particle_id] = cell.pop()

        # for o, c in zip(objects, collected_charge):
        #     o.charge += c
    
    def num_of_particles(self):
        'Return number of particles in total.'
        return sum([len(x) for x in self])

    def num_of_positives(self):
        return np.sum([np.sum([p.q>0 for p in c],dtype=int) for c in self])

    def num_of_negatives(self):
        return np.sum([np.sum([p.q<0 for p in c],dtype=int) for c in self])

    def num_of_conditioned(self, condition):
        '''
        Number of particles satisfying some condition.
        E.g. pop.num_of_conditions(lambda p: p.q<0)
        is equivalent to pop.num_of_negatives()
        '''
        return np.sum([np.sum([cond(p) for p in c],dtype=int) for c in self])

    def save_file(self, fname):
        with open(fname, 'w') as datafile:
            for cell in self:
                for particle in cell:
                    x = '\t'.join([str(x) for x in particle.x])
                    v = '\t'.join([str(v) for v in particle.v])
                    q = particle.q
                    m = particle.m
                    datafile.write("%s\t%s\t%s\t%s\n"%(x,v,q,m))

    def load_file(self, fname):
        nDims = len(self.Ld)
        with open(fname, 'r') as datafile:
            for line in datafile:
                nums = np.array([float(a) for a in line.split('\t')])
                x = nums[0:nDims]
                v = nums[nDims:2*nDims]
                q = nums[2*nDims]
                m = nums[2*nDims+1]
                self.add_particles([x],v,q,m)
