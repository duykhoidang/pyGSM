#!/usr/bin/env python

import time
from collections import OrderedDict, defaultdict
import numpy as np
from numpy.linalg import multi_dot
import elements
from nifty import click, commadash, ang2bohr, bohr2ang, logger
from _math_utils import *
import options
from slots import *
from molecule import Molecule 

ELEMENT_TABLE = elements.ElementData()

CacheWarning = False

class InternalCoordinates(object):

    @staticmethod
    def default_options():
        ''' InternalCoordinates default options.'''

        if hasattr(InternalCoordinates, '_default_options'): return InternalCoordinates._default_options.copy()
        opt = options.Options() 

        opt.add_option(
                key="molecule",
                required=True,
                allowed_types=[Molecule],
                doc='Molecule object'
                )

        opt.add_option(
                key="frozen_atoms",
                value=None,
                required=False,
                doc='Atoms to be left unoptimized/unmoved',
                )

        opt.add_option(
                key='connect',
                value=False,
                allowed_types=[bool],
                doc="Connect the fragments/residues together with a minimum spanning bond,\
                    use for DLC, Don't use for TRIC, (HDLC?).",
                )

        opt.add_option(
                key='addcart',
                value=False,
                allowed_types=[bool],
                doc="Add cartesian coordinates\
                    use to form HDLC ,Don't use for TRIC, DLC.",
                )

        opt.add_option(
                key='addtr',
                value=False,
                allowed_types=[bool],
                doc="Add translation and rotation coordinates\
                    use for TRIC.",
                )

        opt.add_option(
                key='constraints',
                value=None,
                allowed_types=[list],
                doc='A list of Distance,Angle,Torsion constraints (see slots.py),\
                    This is only useful if doing a constrained geometry optimization\
                    since GSM will handle the constraint automatically.'
                )
        opt.add_option(
                key='cVals',
                value=None,
                allowed_types=[list],
                doc='List of Distance,Angle,Torsion constraints values'
                )

        opt.add_option(
                key='print_level',
                value=1,
                required=False,
                allowed_types=[int],
                doc='0-- no printing, 1-- printing')

        InternalCoordinates._default_options = opt
        return InternalCoordinates._default_options.copy()

    @staticmethod
    def from_options(**kwargs):
        """ Returns an instance of this class with default options updated from values in kwargs"""
        return InternalCoordinates(InternalCoordinates.default_options().set_values(kwargs))

    def __init__(self,
            options
            ):

        self.options = options
        self.stored_wilsonB = OrderedDict()
        connect=options['connect']
        addcart=options['addcart']
        addtr=options['addtr']

        if addtr:
            print(" Using TRIC")
            if connect:
                raise RuntimeError(" Intermolecular displacements are defined by translation and rotations! \
                                    Don't add connect!")
        elif addcart:
            print(" Using HDLC")
            if connect:
                raise RuntimeError(" Intermolecular displacements are defined by cartesians! \
                                    Don't add connect!")
        else:
            print(" Using DLC")

    def addConstraint(self, cPrim, cVal):
        raise NotImplementedError("Constraints not supported with Cartesian coordinates")

    def haveConstraints(self):
        raise NotImplementedError("Constraints not supported with Cartesian coordinates")

    def augmentGH(self, xyz, G, H):
        raise NotImplementedError("Constraints not supported with Cartesian coordinates")

    def calcGradProj(self, xyz, gradx):
        raise NotImplementedError("Constraints not supported with Cartesian coordinates")

    def clearCache(self):
        self.stored_wilsonB = OrderedDict()

    def wilsonB(self, xyz):
        """
        Given Cartesian coordinates xyz, return the Wilson B-matrix
        given by dq_i/dx_j where x is flattened (i.e. x1, y1, z1, x2, y2, z2)
        """
        global CacheWarning
        t0 = time.time()
        xhash = hash(xyz.tostring())
        ht = time.time() - t0
        if xhash in self.stored_wilsonB:
            ans = self.stored_wilsonB[xhash]
            return ans
        WilsonB = []
        Der = self.derivatives(xyz)
        for i in range(Der.shape[0]):
            WilsonB.append(Der[i].flatten())
        self.stored_wilsonB[xhash] = np.array(WilsonB)
        if len(self.stored_wilsonB) > 1000 and not CacheWarning:
            logger.warning("\x1b[91mWarning: more than 100 B-matrices stored, memory leaks likely\x1b[0m")
            CacheWarning = True
        ans = np.array(WilsonB)
        return ans

    def GMatrix(self, xyz):
        """
        Given Cartesian coordinates xyz, return the G-matrix
        given by G = BuBt where u is an arbitrary matrix (default to identity)
        """
        Bmat = self.wilsonB(xyz)
        BuBt = np.dot(Bmat,Bmat.T)
        return BuBt

    def GInverse_SVD(self, xyz):
        xyz = xyz.reshape(-1,3)
        # Perform singular value decomposition
        click()
        loops = 0
        while True:
            try:
                G = self.GMatrix(xyz)
                time_G = click()
                U, S, VT = np.linalg.svd(G)
                time_svd = click()
            except np.linalg.LinAlgError:
                logger.warning("\x1b[1;91m SVD fails, perturbing coordinates and trying again\x1b[0m")
                xyz = xyz + 1e-2*np.random.random(xyz.shape)
                loops += 1
                if loops == 10:
                    raise RuntimeError('SVD failed too many times')
                continue
            break
        # print "Build G: %.3f SVD: %.3f" % (time_G, time_svd),
        V = VT.T
        UT = U.T
        Sinv = np.zeros_like(S)
        LargeVals = 0
        for ival, value in enumerate(S):
            # print "%.5e % .5e" % (ival,value)
            if np.abs(value) > 1e-6:
                LargeVals += 1
                Sinv[ival] = 1/value
        # print "%i atoms; %i/%i singular values are > 1e-6" % (xyz.shape[0], LargeVals, len(S))
        Sinv = np.diag(Sinv)
        Inv = multi_dot([V, Sinv, UT])
        return Inv

    def GInverse_EIG(self, xyz):
        xyz = xyz.reshape(-1,3)
        click()
        G = self.GMatrix(xyz)
        time_G = click()
        Gi = np.linalg.inv(G)
        time_inv = click()
        # print "G-time: %.3f Inv-time: %.3f" % (time_G, time_inv)
        return Gi

    def checkFiniteDifference(self, xyz):
        xyz = xyz.reshape(-1,3)
        Analytical = self.derivatives(xyz)
        FiniteDifference = np.zeros_like(Analytical)
        h = 1e-5
        for i in range(xyz.shape[0]):
            for j in range(3):
                x1 = xyz.copy()
                x2 = xyz.copy()
                x1[i,j] += h
                x2[i,j] -= h
                PMDiff = self.calcDiff(x1,x2)
                FiniteDifference[:,i,j] = PMDiff/(2*h)
        for i in range(Analytical.shape[0]):
            logger.info("IC %i/%i : %s" % (i, Analytical.shape[0], self.Internals[i]))
            lines = [""]
            maxerr = 0.0
            for j in range(Analytical.shape[1]):
                lines.append("Atom %i" % (j+1))
                for k in range(Analytical.shape[2]):
                    error = Analytical[i,j,k] - FiniteDifference[i,j,k]
                    if np.abs(error) > 1e-5:
                        color = "\x1b[91m"
                    else:
                        color = "\x1b[92m"
                    lines.append("%s % .5e % .5e %s% .5e\x1b[0m" % ("xyz"[k], Analytical[i,j,k], FiniteDifference[i,j,k], color, Analytical[i,j,k] - FiniteDifference[i,j,k]))
                    if maxerr < np.abs(error):
                        maxerr = np.abs(error)
            if maxerr > 1e-5:
                logger.info('\n'.join(lines))
            else:
                logger.info("Max Error = %.5e" % maxerr)
        logger.info("Finite-difference Finished")

    def calcGrad(self, xyz, gradx):
        q0 = self.calculate(xyz)
        Ginv = self.GInverse(xyz)
        Bmat = self.wilsonB(xyz)
        # Internal coordinate gradient
        # Gq = np.matrix(Ginv)*np.matrix(Bmat)*np.matrix(gradx)
        Gq = multi_dot([Ginv, Bmat, gradx])
        return Gq

    def readCache(self, xyz, dQ):
        if not hasattr(self, 'stored_xyz'):
            return None
        xyz = xyz.flatten()
        dQ = dQ.flatten()
        if np.linalg.norm(self.stored_xyz - xyz) < 1e-10:
            if np.linalg.norm(self.stored_dQ - dQ) < 1e-10:
                return self.stored_newxyz
        return None

    def writeCache(self, xyz, dQ, newxyz):
        xyz = xyz.flatten()
        dQ = dQ.flatten()
        newxyz = newxyz.flatten()
        self.stored_xyz = xyz.copy()
        self.stored_dQ = dQ.copy()
        self.stored_newxyz = newxyz.copy()

    def newCartesian(self, xyz, dQ, verbose=True):
        cached = self.readCache(xyz, dQ)
        if cached is not None:
            # print "Returning cached result"
            return cached
        xyz1 = xyz.copy()
        dQ1 = dQ.copy()
        # Iterate until convergence:
        microiter = 0
        ndqs = []
        rmsds = []
        self.bork = False
        # Damping factor
        damp = 1.0
        # Function to exit from loop
        def finish(microiter, rmsdt, ndqt, xyzsave, xyz_iter1):
            if ndqt > 1e-1:
                if verbose: logger.info("Failed to obtain coordinates after %i microiterations (rmsd = %.3e |dQ| = %.3e)" % (microiter, rmsdt, ndqt))
                self.bork = True
                self.writeCache(xyz, dQ, xyz_iter1)
                return xyz_iter1.flatten()
            elif ndqt > 1e-3:
                if verbose: logger.info("Approximate coordinates obtained after %i microiterations (rmsd = %.3e |dQ| = %.3e)" % (microiter, rmsdt, ndqt))
            else:
                if verbose: logger.info("Cartesian coordinates obtained after %i microiterations (rmsd = %.3e |dQ| = %.3e)" % (microiter, rmsdt, ndqt))
            self.writeCache(xyz, dQ, xyzsave)
            return xyzsave.flatten()
        fail_counter = 0
        while True:
            microiter += 1
            Bmat = self.wilsonB(xyz1)
            Ginv = self.GInverse(xyz1)
            # Get new Cartesian coordinates
            dxyz = damp*multi_dot([Bmat.T,Ginv,dQ1.T])
            xyz2 = xyz1 + np.array(dxyz).flatten()
            if microiter == 1:
                xyzsave = xyz2.copy()
                xyz_iter1 = xyz2.copy()
            # Calculate the actual change in internal coordinates
            dQ_actual = self.calcDiff(xyz2, xyz1)
            rmsd = np.sqrt(np.mean((np.array(xyz2-xyz1).flatten())**2))
            ndq = np.linalg.norm(dQ1-dQ_actual)
            if len(ndqs) > 0:
                if ndq > ndqt:
                    if verbose: logger.info("Iter: %i Err-dQ (Best) = %.5e (%.5e) RMSD: %.5e Damp: %.5e (Bad)" % (microiter, ndq, ndqt, rmsd, damp))
                    damp /= 2
                    fail_counter += 1
                    # xyz2 = xyz1.copy()
                else:
                    if verbose: logger.info("Iter: %i Err-dQ (Best) = %.5e (%.5e) RMSD: %.5e Damp: %.5e (Good)" % (microiter, ndq, ndqt, rmsd, damp))
                    fail_counter = 0
                    damp = min(damp*1.2, 1.0)
                    rmsdt = rmsd
                    ndqt = ndq
                    xyzsave = xyz2.copy()
            else:
                if verbose: logger.info("Iter: %i Err-dQ = %.5e RMSD: %.5e Damp: %.5e" % (microiter, ndq, rmsd, damp))
                rmsdt = rmsd
                ndqt = ndq
            ndqs.append(ndq)
            rmsds.append(rmsd)
            # Check convergence / fail criteria
            if rmsd < 1e-6 or ndq < 1e-6:
                return finish(microiter, rmsdt, ndqt, xyzsave, xyz_iter1)
            if fail_counter >= 5:
                return finish(microiter, rmsdt, ndqt, xyzsave, xyz_iter1)
            if microiter == 50:
                return finish(microiter, rmsdt, ndqt, xyzsave, xyz_iter1)
            # Figure out the further change needed
            dQ1 = dQ1 - dQ_actual
            xyz1 = xyz2.copy()
            
