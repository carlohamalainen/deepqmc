import logging
import operator
import pickle
from collections import namedtuple
from copy import deepcopy
from functools import partial
from itertools import count
from pathlib import Path

import h5py
import jax
import jax.numpy as jnp
import tensorboard.summary
from tqdm.auto import tqdm
from uncertainties import ufloat

from .ewm import ewm
from .fit import fit_wf, init_fit
from .log import H5LogTable
from .physics import pairwise_self_distance
from .sampling import equilibrate
from .wf.base import state_callback

__all__ = ['train']

log = logging.getLogger(__name__)


def train(
    hamil,
    ansatz,
    opt,
    sampler,
    workdir=None,
    state_callback=state_callback,
    *,
    steps,
    sample_size,
    seed,
    max_restarts=3,
    **kwargs,
):
    r"""Train or evaluate a JAX wave function model.

    It initializes and equilibrates the MCMC sampling of the wave function ansatz,
    then optimizes or samples it using the variational principle. It optionally
    saves checkpoints and rewinds the training/evaluation if an error is encountered.
    If an optimizer is supplied, the Ansatz is optimized, otherwise the Ansatz is
    only sampled.

    Args:
        hamil (~jax.hamil.Hamiltonian): the Hamiltonian of the physical system.
        ansatz (~jax.wf.WaveFunction): the wave function ansatz.
        opt (``kfac_jax`` or ``optax`` optimizers, or :data:`None`): the optimizer.
            Possible values are:

            - :class:`kfac_jax.Optimizer`: the KFAC optimizer is used
            - an :data:`optax` optimizer: the supplied :data:`optax` optimizer
                is used.
            - :data:`None`: no optimizer is used, e.g. the evaluation of the Ansatz
                is performed.

        sampler (~jax.sampling.Sampler): a sampler instance
        workdir (str): optional, path, where results and checkpoints should be saved.
        state_callback (Callable): optional, a function processing the :class:`haiku`
            state of the wave function ansatz.
        steps (int): optional, number of optimization steps.
        sample_size (int): the number of samples considered in a batch
        seed (int): the seed used for PRNG.
        max_restarts (int): optional, the maximum number of times the training is
                retried before a :class:`NaNError` is raised.
        kwargs (dict): optional, extra arguments passed to the :func:`~.fit.fit_wf`
            function.
    """

    ewm_state = ewm()
    rng = jax.random.PRNGKey(seed)
    mode = 'evaluate' if opt is None else 'train'
    if workdir:
        workdir = f'{workdir}/{mode}'
        chkpts = CheckpointStore(workdir)
        writer = tensorboard.summary.Writer(workdir)
        log.debug('Setting up HDF5 file...')
        h5file = h5py.File(f'{workdir}/result.h5', 'a', libver='v110')
        h5file.swmr_mode = True
        table = H5LogTable(h5file)
        h5file.flush()
    pbar = None
    try:
        params, smpl_state = init_fit(
            rng, hamil, ansatz, sampler, sample_size, state_callback
        )
        num_params = jax.tree_util.tree_reduce(
            operator.add, jax.tree_map(lambda x: x.size, params)
        )
        log.info(f'Number of model parameters: {num_params}')
        log.info('Equilibrating sampler...')
        pbar = tqdm(count(), desc='equilibrate', disable=None)
        for _, smpl_state, smpl_stats in equilibrate(  # noqa: B007
            rng,
            partial(ansatz.apply, params),
            sampler,
            smpl_state,
            lambda r: pairwise_self_distance(r).mean(),
            pbar,
            state_callback,
            block_size=10,
        ):
            pbar.set_postfix(tau=f'{smpl_state["tau"].item():5.3f}')
            # TODO
            # if workdir:
            #     for k, v in smpl_stats.items():
            #         writer.add_scalar(k, v, step)
        pbar.close()
        log.info('Start training')
        pbar = tqdm(range(steps), desc='train', disable=None)
        best_ene = None
        train_state = params, None, smpl_state
        for _ in range(max_restarts):
            for step, train_state, E_loc, stats in fit_wf(  # noqa: B007
                rng,
                hamil,
                ansatz,
                opt,
                sampler,
                sample_size,
                pbar,
                state_callback,
                train_state,
                **kwargs,
            ):
                if jnp.isnan(train_state.sampler['psi'].log).any():
                    log.warn('Restarting due to a NaN...')
                    step, train_state = chkpts.last
                    pbar.close()
                    pbar = tqdm(range(step, steps), desc='train', disable=None)
                    break
                ewm_state = ewm(stats['E_loc/mean'], ewm_state)
                stats = {
                    'energy/ewm': ewm_state.mean,
                    'energy/ewm_error': jnp.sqrt(ewm_state.sqerr),
                    **stats,
                }
                ene = ufloat(stats['energy/ewm'], stats['energy/ewm_error'])
                if ene.s:
                    pbar.set_postfix(E=f'{ene:S}')
                    if best_ene is None or ene.n < best_ene.n - 3 * ene.s:
                        best_ene = ene
                        log.info(f'Progress: {step + 1}/{steps}, energy = {ene:S}')
                if workdir:
                    if mode == 'train':
                        chkpts.update(stats['E_loc/std'], train_state)
                    table.row['E_loc'] = E_loc
                    table.row['E_ewm'] = ewm_state.mean
                    table.row['sign_psi'] = train_state.sampler['psi'].sign
                    table.row['log_psi'] = train_state.sampler['psi'].log
                    for k, v in stats.items():
                        writer.add_scalar(k, v, step)
            return train_state
    finally:
        if pbar:
            pbar.close()
        if workdir:
            chkpts.close()
            writer.close()
            h5file.close()


Checkpoint = namedtuple('Checkpoint', 'step loss path')


class CheckpointStore:
    PATTERN = 'chkpt-{}.pt'

    def __init__(self, workdir, size=3, min_interval=100, threshold=0.95):
        self.workdir = Path(workdir)
        for p in self.workdir.glob(self.PATTERN.format('*')):
            p.unlink()
        self.size = size
        self.min_interval = min_interval
        self.threshold = threshold
        self.chkpts = []
        self.step = 0
        self.buffer = None

    def update(self, loss, state):
        self.step += 1
        self.buffer = deepcopy(state)
        if (
            self.step < self.min_interval
            or self.chkpts
            and (
                self.step < self.min_interval + self.chkpts[-1].step
                or loss > self.threshold * self.chkpts[-1].loss
            )
        ):
            return
        path = self.dump(state)
        self.chkpts.append(Checkpoint(self.step, loss, path))
        while len(self.chkpts) > self.size:
            self.chkpts.pop(0).path.unlink()

    def dump(self, state):
        path = self.workdir / self.PATTERN.format(self.step)
        with path.open('wb') as f:
            pickle.dump(state, f)
        return path

    def close(self):
        if self.buffer is not None:
            self.dump(self.buffer)

    @property
    def last(self):
        chkpt = self.chkpts[-1]
        with chkpt.path.open('rb') as f:
            return chkpt.step, pickle.load(f)
