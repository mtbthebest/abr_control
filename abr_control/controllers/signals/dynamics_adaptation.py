import glob
import redis
import struct
import os
import time
import numpy as np
import scipy.special
import logging
r = redis.StrictRedis(host='127.0.0.1')

# import abr_control.utils.os_utils
from abr_control.utils.paths import cache_dir
from .signal import Signal

try:
    import nengo
except ImportError:
    raise Exception('Nengo module needs to be installed to ' +
                    'use adaptive dynamics.')


class DynamicsAdaptation(Signal):
    """ An implementation of nonlinear dynamics adaptation using Nengo,
    as described in (DeWolf, Stewart, Slotine, and Eliasmith, 2016)

    The model learns to account for unknown / unmodelled external or
    internal forces, using an efferent copy of the control signal to train.

    Parameters
    ----------
    robot_config : class instance
        contains all relevant information about the arm
        such as: number of joints, number of links, mass information etc.
    n_neurons : int, optional (Default: 1000)
        number of neurons per adaptive population
    n_ensembles : int, option (Default: 1)
        number of ensembles of n_neurons number of neurons
    pes_learning_rate : float, optional (Default: 1e-6)
        controls the speed of neural adaptation
    intercepts list : list of two floats, optional (Default: (0.5, 1.0))
        voltage threshold range for neurons
    spiking : boolean, optional (Default: False)
        use spiking or rate mode neurons
    weights_file : string, optional (Default: None)
        path to file where learned weights are saved
    backend : string, optional (Default: nengo)
        {'nengo', 'nengo_ocl', 'nengo_spinnaker'}
    use_dq : boolean, optional (Default: False)
        set True to pass in position and velocity,
        False if only passing in position
    session: int, optional (Default: None)
        if doing multiple sessions of n runs to average over.
        if set to None it will search for the most recent session
        to save to, saving to 'session0' if none exist
    run: int, optional (Default: None)
        the current run number. This value will automatically
        increment based on the last run saved in the test_name
        folder. The user can specify a run if they desire to
        overwrite a previous run. If set to None then the test_name
        folder will be searched for the next run to save as
    test_name: string, optional (Default: 'test')
        the test name to save the weights under
    autoload: boolean, optional (Default: False)
        set true if you would like the script to check the 'test_name'
        directory for the most recent saved weights, False to start
        with a zeros array of weights, or a specified 'weights_file'
    function:
    redis_comm: boolean, optional (Default: False)
        True to send spiking information to redis for display purposes
    """
    #TODO add description of function

    def __init__(self, n_input, n_output, n_neurons=1000, n_ensembles=1,
                 seed=None, pes_learning_rate=1e-6, intercepts=(0.5, 1.0),
                 weights_file=None, backend='nengo', session=None,
                 run=None, test_name='test', autoload=False,
                 function=None, redis_comm=False, encoders=None, **kwargs):

        self.input_signal = np.zeros(n_input)
        self.training_signal = np.zeros(n_output)
        self.output = np.zeros(n_output)

        # if a weights_file is not specified and auto_load is desired,
        # check the test_name directory for the most recent weights
        if weights_file is None and autoload:
            weights_file = self.load_weights(
                session=session, run=run, test_name=test_name)

        if weights_file is None:
            weights_file = ''

        self.nengo_model = nengo.Network(seed=seed)

        small_tau = 0.005
        if backend == 'nengo_spinnaker':
            tau = 0.015  # TODO: this could be more exact
            # i.e. 0.005 * control_loop_time / 0.001 ... i think
        # NOTE: SHOULD THE FILTER ON THE ERROR SIGNAL BE small_tau NOT big_tau?
        big_tau = small_tau * 2  # filter on the training signal
        self.nengo_model.config[nengo.Connection].synapse = small_tau

        with self.nengo_model:

            def input_signals_func(t):
                """ Get the input and -1 * training signal """
                # return np.hstack([self.input_signal, -self.training_signal])
                return self.input_signal
            input_signals = nengo.Node(
                # input_signals_func, size_out=n_input+n_output)
                input_signals_func, size_out=n_input)

            def training_signals_func(t):
                return -self.training_signal
            training_signals = nengo.Node(
                training_signals_func, size_out=n_output)

            # make the adaptive population output accessible
            def output_func(t, x):
                """ stores the adaptive output for use in control() """
                self.output = np.copy(x)
            output = nengo.Node(output_func, size_in=n_output, size_out=0)

            # specify intercepts such that neurons are
            # active throughout the whole range specified
            intercepts = AreaIntercepts(
                dimensions=n_input,
                base=nengo.dists.Uniform(intercepts[0], intercepts[1]))
            #intercepts = nengo.dists.Uniform(intercepts[0], intercepts[1])

            self.adapt_ens = []
            self.conn_learn = []

            for ii in range(n_ensembles):
                self.adapt_ens.append(nengo.Ensemble(n_neurons=n_neurons, dimensions=n_input,
                                           intercepts=intercepts,#neuron_type=nengo.RectifiedLinear(),
                                           #max_rates=nengo.dists.Uniform(0, 1), **kwargs))
                                           radius=np.sqrt(n_input), **kwargs))
                                           #**kwargs))
                print('*** ENSEMBLE %i ***' % ii)

                try:
                    # if the NengoLib is installed, use it
                    # to optimize encoder placement
                    import nengolib
                    if encoders is None:
                        self.adapt_ens[ii].encoders = (
                            nengolib.stats.ScatteredHypersphere(surface=True))
                        print('NengoLib used to optimize encoders placement')
                    else:
                        self.adapt_ens[ii].encoders = encoders
                        print('Using user defined encoder values')
                except ImportError:
                    print('Nengo lib not installed, encoder ' +
                          'placement will be sub-optimal.')

                # hook up input signal to adaptive population to provide context
                # nengo.Connection(input_signals[:n_input], self.adapt_ens, synapse=0.005)
                nengo.Connection(input_signals, self.adapt_ens[ii]) #, synapse=0.005)

                # load weights from file if they exist, otherwise use zeros
                if weights_file == '~':
                    weights_file = os.path.expanduser(weights_file)
                print('Backend: ', backend)
                print('Weights file: %s' % weights_file)
                if not os.path.isfile('%s' % weights_file):
                    print('No weights found, starting with zeros')
                    transform = np.zeros((n_output, self.adapt_ens[ii].n_neurons))

                # set up learning connections
                if function is not None and (weights_file is None or weights_file is ''):
                    print("Using mass estimation function")
                    self.conn_learn.append(nengo.Connection(
                                           self.adapt_ens[ii], output,
                                           learning_rule_type=nengo.PES(pes_learning_rate),
                                           function=function))
                else:
                    if backend == 'nengo_spinnaker':
                        if os.path.isfile('%s' % weights_file):
                            if n_ensembles == 1:
                                transform = np.squeeze(np.load(weights_file)['weights']).T
                            else:
                                transform = np.squeeze(np.load(weights_file)['weights'])[ii].T
                            print('Loading weights: \n', transform)
                            print('Loaded weights all zeros: ', np.allclose(transform, 0))



                        print(transform.T.shape)

                        self.conn_learn.append(nengo.Connection(
                                               self.adapt_ens[ii], output,
                                               function=lambda x: np.zeros(n_output),
                                               learning_rule_type=nengo.PES(pes_learning_rate),
                                               solver=DummySolver(transform.T)))
                    else:

                        if os.path.isfile('%s' % weights_file):
                            if n_ensembles ==1:
                                transform = np.load(weights_file)['weights']
                            else:
                                transform = np.load(weights_file)['weights'][ii]
                            # remove third dimension if present
                            if len(transform.shape) > 2:
                                transform = np.squeeze(transform)
                            print('Loading weights: \n', transform)
                            print('Loaded weights all zeros: ', np.allclose(transform, 0))

                        self.conn_learn.append(nengo.Connection(
                                               self.adapt_ens[ii].neurons, output,
                                               learning_rule_type=nengo.PES(pes_learning_rate),
                                               transform=transform))

                # hook up the training signal to the learning rule
                nengo.Connection(
                    # input_signals[n_input:], self.conn_learn.learning_rule,
                    training_signals, self.conn_learn[ii].learning_rule,
                    synapse=big_tau)




            # TODO: add parameter to use redis display as an option
            if backend != 'nengo_spinnaker' and redis_comm:
                    # Send spikes via redis
                    def send_spikes(t, x):
                            v = np.where(x != 0)[0]
                            if len(v) > 0:
                                msg = struct.pack('%dI' % len(v), *v)
                            else:
                                msg = ''
                            r.set('spikes', msg)
                            self.activity = x
                    source_node = nengo.Node(send_spikes, size_in=n_neurons)
                    nengo.Connection(
                        self.adapt_ens[0].neurons[:n_neurons],
                        source_node,
                        synapse=None)
            def save_x(t, x):
                self.x = x
            x_node = nengo.Node(save_x, size_in=n_input)
            nengo.Connection(self.adapt_ens[0], x_node, synapse=big_tau)





        nengo.cache.DecoderCache().invalidate()
        if backend == 'nengo':
            self.sim = nengo.Simulator(self.nengo_model, dt=.001)
        elif backend == 'nengo_ocl':
            try:
                import nengo_ocl
            except ImportError:
                raise Exception('Nengo OCL not installed, ' +
                                'cannot use this backend.')
            import pyopencl as cl
            # Here, the context would be to use all devices from platform [0]
            ctx = cl.Context(cl.get_platforms()[0].get_devices())
            self.sim = nengo_ocl.Simulator(self.nengo_model, context=ctx, dt=.001)
        elif backend == 'nengo_spinnaker':
            try:
                import nengo_spinnaker
            except ImportError:
                raise Exception('Nengo SpiNNaker not installed, ' +
                                'cannot use this backend.')
            # TODO: parameterize this
            # turn on debug printing
            # logging.basicConfig(level=logging.DEBUG)
            self.sim = nengo_spinnaker.Simulator(self.nengo_model)
            # start running the spinnaker model
            self.sim.async_run_forever()
        else:
            raise Exception('Invalid backend specified')
        self.backend = backend

    def generate(self, input_signal, training_signal):
        """ Generates the control signal

        Parameters
        ----------
        q : numpy.array
            the current joint angles [radians]
        dq : numpy.array
            the current joint velocities [radians/second]
        training_signal : numpy.array
            the learning signal to drive adaptation
        """

        # store local copies to feed in to the adaptive population
        self.input_signal = input_signal
        self.training_signal = training_signal

        # run the simulation t generate the adaptive signal
        if self.backend == 'nengo' or self.backend == 'nengo_ocl':
            self.sim.run(time_in_seconds=.001, progress_bar=False)
        elif self.backend == 'nengo_spinnaker':
            # update spinnaker inputs
            self.sim.async_update()
        else:
            self.sim.run(time_in_seconds=.001)

        return self.output

    def weights_location(self, session=None, run=None, test_name='test'):
        """ Search for most recent saved weights

        Searches the specified test_name for the highest numbered run in the
        highest numbered session, unless otherwise specified. Returns the file
        location and the number of the most recent run.

        Parameters
        ----------
        session: int, optional (Default: None)
            if doing multiple sessions of n runs to average over.
            if set to None it will search for the most recent session
            to save to, saving to 'session0' if none exist
        run: int, optional (Default: None)
            the current run number. This value will automatically
            increment based on the last run saved in the test_name
            folder. The user can specify a run if they desire to
            overwrite a previous run. If set to None then the test_name
            folder will be searched for the next run to save as
        test_name: string, optional (Default: 'test')
            the test name to save the weights under
        """

        test_name = cache_dir + '/saved_weights/' + test_name

        # if session is not None, then the user has specified what session to save the
        # current weights in
        if session is not None:
            test_name += '/session%i' % session
        # If no session is specified, check if there is already one created, if not
        # then save the run to 'session0'
        else:
            prev_sessions = glob.glob(test_name + '/session*')
            run_cache = []
            if prev_sessions:
                test_name = max(prev_sessions)
            else:
                test_name += '/session0'

        # check if the provided test_name exists, if not, create it
        if not os.path.exists(test_name):
            print("The provided directory: '%s' does not exist, creating folder..."
                % test_name)
            os.makedirs(test_name)

        # if no run is specified, check what the most recent run saved is and save as
        # the next one in the series. If no run exists then save it as 'run0'
        # if saving, if loading throw an error that no weights file exists
        if run is None:
            prev_runs = glob.glob(test_name + '/*.npz')
            run_cache = []
            if prev_runs:
                for ii in range(0, len(prev_runs)):
                    run_cache.append(int(prev_runs[ii][prev_runs[ii].rfind('/')+4:prev_runs[ii].find('.npz')]))
                run_num = max(run_cache)
            else:
                run_num = -1
        else:
            # save as the run specified by the user
            run_num = run

        return [test_name, run_num]

    def save_weights(self, **kwargs):
        """ Save the current weights to the specified test_name folder

        Save weights for individual runs. A group of runs is
        classified as a session. Multiple sessions can then be used
        to average over a set of learned runs. If session or run are set to None
        then the test_name location will be searched for the highest numbered
        folder and file respectively
        """

        [test_name, run_num] = self.weights_location(**kwargs)

        print('saving weights as run%i'% (run_num+1))
        if self.backend == 'nengo_spinnaker':
            import nengo_spinnaker.utils.learning
            #self.sim.close()
            #time.sleep(5)
            print('save location: ', test_name + '/run%i' % (run_num +1))
            np.savez_compressed(
                test_name + '/run%i' % (run_num + 1),
                weights=([nengo_spinnaker.utils.learning.get_learnt_decoders(
                         self.sim, ens) for ens in self.adapt_ens]))
            # print('Spinnaker output: ', nengo_spinnaker.utils.learning.get_learnt_decoders(self.sim,
            #     self.adapt_ens[0]))

        else:
            np.savez_compressed(
                test_name + '/run%i' % (run_num + 1),
                weights=([self.sim.signals[self.sim.model.sig[conn]['weights']]
                         for conn in self.conn_learn]))

    def load_weights(self, **kwargs):
        """ Loads the most recently saved weights unless otherwise specified

        Checks test_name to load the most recent set of weights, unless
        otherwise specified in session and run. Assumes the most recent is the
        highest session and run number.
        """

        [test_name, run_num] = self.weights_location(**kwargs)

        if run_num == -1:
            print('No weights found in the specified directory...')
            weights_file = None
        else:
            weights_file = test_name + '/run%i.npz' % run_num
        return weights_file

class DummySolver(nengo.solvers.Solver):
    """ A Nengo weights solver that returns a provided set of weights.
    """
    def __init__(self, fixed):
        self.fixed = fixed
        self.weights = False

    def __call__(self, A, Y, rng=None, E=None):
        return self.fixed, {}


class AreaIntercepts(nengo.dists.Distribution):
    """ Generate an optimally distributed set of intercepts in
    high-dimensional space.
    """
    dimensions = nengo.params.NumberParam('dimensions')
    base = nengo.dists.DistributionParam('base')

    def __init__(self, dimensions, base=nengo.dists.Uniform(-1, 1)):
        super(AreaIntercepts, self).__init__()
        self.dimensions = dimensions
        self.base = base

    def __repr(self):
        return ("AreaIntercepts(dimensions=%r, base=%r)" %
                (self.dimensions, self.base))

    def transform(self, x):
        sign = 1
        if x > 0:
            x = -x
            sign = -1
        return sign * np.sqrt(1 - scipy.special.betaincinv(
            (self.dimensions + 1) / 2.0, 0.5, x + 1))

    def sample(self, n, d=None, rng=np.random):
        s = self.base.sample(n=n, d=d, rng=rng)
        for i in range(len(s)):
            s[i] = self.transform(s[i])
        return s
