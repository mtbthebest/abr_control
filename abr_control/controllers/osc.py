import numpy as np

from . import controller


class OSC(controller.Controller):
    """ Implements an operational space controller (OSC)

    Parameters
    ----------
    robot_config : class instance
        contains all relevant information about the arm
        such as: number of joints, number of links, mass information etc.
    kp : float, optional (Default: 1)
        proportional gain term
    kv : float, optional (Default: None)
        derivative gain term, a good starting point is sqrt(kp)
    ki : float, optional (Default: 0)
        integral gain term
    vmax : float, optional (Default: 0.5)
        The max allowed velocity of the end-effector [meters/second].
        If the control signal specifies something above this
        value it is clipped, if set to None no clipping occurs
    null_control : boolean, optional (Default: True)
        Apply a secondary control signal which
        drives the arm to specified resting joint angles without
        affecting the movement of the end-effector
    use_g : boolean, optional (Default: True)
        calculate and compensate for the effects of gravity
    use_C : boolean, optional (Default: False)
        calculate and compensate for the Coriolis and
        centripetal effects of the arm
    use_dJ : boolean, optional (Default: False)
        use the Jacobian derivative wrt time

    Attributes
    ----------
    nkp : float
        proportional gain term for null controller
    nkv : float
        derivative gain term for null controller
    integrated_error : float list, optional (Default: None)
        task-space integrated error term
    """
    def __init__(self, robot_config, kp=1, kv=None, ki=0, vmax=0.5,
                 null_control=True, use_g=True, use_C=False, use_dJ=False,
                 integrated_error=None):

        super(OSC, self).__init__(robot_config)

        self.kp = kp
        self.kv = np.sqrt(self.kp) if kv is None else kv
        self.ki = ki
        self.vmax = vmax
        self.lamb = self.kp / self.kv
        self.null_control = null_control
        self.use_g = use_g
        self.use_C = use_C
        self.use_dJ = use_dJ

        if integrated_error is None:
            self.integrated_error = np.array([0.0, 0.0, 0.0])
        else:
            self.integrated_error = integrated_error

        # null_indices is a mask for identifying which joints have REST_ANGLES
        self.null_indices = ~np.isnan(self.robot_config.REST_ANGLES)
        self.dq_des = np.zeros(self.robot_config.N_JOINTS)
        self.IDENTITY_N_JOINTS = np.eye(self.robot_config.N_JOINTS)
        # null space filter gains
        self.nkp = self.kp * .1
        self.nkv = np.sqrt(self.nkp)

        # run the controller once to generate any functions we might be missing
        # to avoid a long delay in the control loop
        if hasattr(robot_config, 'OFFSET'):
            offset = robot_config.OFFSET
        else:
            print('robot config has no offset attribute, using zeros')
            offset = [0,0,0]

        self.generate(np.zeros(robot_config.N_JOINTS),
                      np.zeros(robot_config.N_JOINTS),
                      np.zeros(3),
                      offset=offset)

    @property
    def params(self):
        params = {'source': 'OSC',
                  'kp': self.kp,
                  'kv': self.kv,
                  'ki': self.ki,
                  'vmax': self.vmax,
                  'lamb': self.lamb,
                  'null_control': self.null_control,
                  'use_g': self.use_g,
                  'use_C': self.use_C,
                  'use_dJ': self.use_dJ,
                  'nkv': self.nkv,
                  'nkp': self.nkp}
        return params

    def generate(self, q, dq,
                 target_pos, target_vel=np.zeros(3),
                 ref_frame='EE', offset=[0, 0, 0], ee_force=None):
        """ Generates the control signal to move the EE to a target

        Parameters
        ----------
        q : float numpy.array
            current joint angles [radians]
        dq : float numpy.array
            current joint velocities [radians/second]
        target_pos : float numpy.array
            desired joint angles [radians]
        target_vel : float numpy.array, optional (Default: numpy.zeros)
            desired joint velocities [radians/sec]
        ref_frame : string, optional (Default: 'EE')
            the point being controlled, default is the end-effector.
        offset : list, optional (Default: [0, 0, 0])
            point of interest inside the frame of reference [meters]
        ee_force: float array, Optional, (Default: None)
            if there are any additional forces to add in task space,
            add them here
        """

        # calculate the end-effector position information
        xyz = self.robot_config.Tx(ref_frame, q, x=offset)
        self.Tx = xyz

        # calculate the Jacobian for the end effector
        J = self.robot_config.J(ref_frame, q, x=offset)
        # isolate position component of Jacobian
        J = J[:3]

        # calculate the inertia matrix in joint space
        M = self.robot_config.M(q)
        self.M = M

        # calculate the inertia matrix in task space
        M_inv = np.linalg.inv(M)
        self.M_inv = M_inv
        # calculate the Jacobian for end-effector with no offset
        self.ref_frame = ref_frame
        self.q = q
        Mx_inv = np.dot(J, np.dot(M_inv, J.T))
        self.Mx_inv = Mx_inv
        if np.linalg.det(M) != 0:
            Mx = np.linalg.inv(Mx_inv)
            self.Mx_non_singular = Mx_inv
        else:
            # using the rcond to set singular values < thresh to 0
            # is slightly faster than doing it manually with svd
            # singular values < (rcond * max(singular_values)) set to 0
            Mx = np.linalg.pinv(Mx_inv, rcond=.04)
            self.Mx_non_singular = None
        self.Mx = Mx

        u_task = np.zeros(3)  # task space control signal

        # calculate the position error
        x_tilde = np.array(xyz - target_pos)

        if self.vmax is not None:
            # implement velocity limiting
            sat = self.vmax / (self.lamb * np.abs(x_tilde))
            if np.any(sat < 1):
                index = np.argmin(sat)
                unclipped = self.kp * x_tilde[index]
                clipped = self.kv * self.vmax * np.sign(x_tilde[index])
                scale = np.ones(3, dtype='float32') * clipped / unclipped
                scale[index] = 1
            else:
                scale = np.ones(3, dtype='float32')

            self.dx = np.dot(J, dq)
            u_task[:3] = -self.kv * (self.dx - target_vel -
                                     np.clip(sat / scale, 0, 1) *
                                     -self.lamb * scale * x_tilde)
            self.u_vmax = u_task
        else:
            # generate (x,y,z) force without velocity limiting)
            u_task[:3] = -self.kp * x_tilde
            self.u_vmax = u_task
            self.dx=None

        if self.use_dJ:
            # add in estimate of current acceleration
            dJ = self.robot_config.dJ(ref_frame, q=q, dq=dq)
            # apply mask
            dJ = dJ[:3]
            u_task -= np.dot(dJ, dq)
            self.u_dj = u_task

        if self.ki != 0:
            # add in the integrated error term
            self.integrated_error += x_tilde
            u_task -= self.ki * self.integrated_error

        # add in any specified additional task space force
        if ee_force is not None:
            u_task += ee_force

        # incorporate task space inertia matrix
        self.u_Mx = np.dot(Mx, u_task)
        u = np.dot(J.T, np.dot(Mx, u_task))
        self.u_inertia = u

        if self.vmax is None:
            u -= np.dot(M, dq)

        if self.use_C:
            # add in estimation of full centrifugal and Coriolis effects
            u -= self.robot_config.c(q=q, dq=dq)

        # store the current control signal u for training in case
        # dynamics adaptation signal is being used
        # NOTE: training signal should not include gravity compensation
        self.training_signal = np.copy(u)

        # cancel out effects of gravity
        if self.use_g:
            u -= self.robot_config.g(q=q)
            self.u_g = u

        if self.null_control:
            # calculated desired joint angle acceleration using rest angles
            q_des = ((self.robot_config.REST_ANGLES - q + np.pi) %
                     (np.pi * 2) - np.pi)
            q_des[~self.null_indices] = 0.0
            self.dq_des[self.null_indices] = dq[self.null_indices]

            u_null = np.dot(M, (self.nkp * q_des - self.nkv * self.dq_des))

            Jbar = np.dot(M_inv, np.dot(J.T, Mx))
            null_filter = (self.IDENTITY_N_JOINTS - np.dot(J.T, Jbar.T))

            u += np.dot(null_filter, u_null)

        return u
