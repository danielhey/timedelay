import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from astropy.stats import LombScargle
from tqdm import tqdm

class TimeDelay():

    def __init__(self, times, mags, freqs=None, min_freq=5, max_freq=49,**kwargs):

        self.times = times
        self.mags = mags

        if freqs is None:
            freqs = self.estimate_frequencies(**kwargs)
            freqs = freqs[freqs<max_freq]
            freqs = freqs[freqs>min_freq]
        self.freqs = freqs
        self.nu = self.freqs

    @staticmethod
    def from_archive(target, **kwargs):
        """Instantiates a TimeDelay object from target KIC ID by downloading
        photometry from MAST. 
        Args:
            target: (string) target ID (i.e. 'KIC9651065')
            **kwargs: Optional args to pass to TimeDelay
        """
        try:
            from lightkurve import KeplerLightCurveFile
        except ImportError:
            raise ImportError('LightKurve package is required for MAST.')

        lcs = KeplerLightCurveFile.from_archive(target, quarter='all', 
                                                cadence='long')
        lc = lcs[0].PDCSAP_FLUX.remove_nans()
        lc.flux = -2.5 * np.log10(lc.flux)
        lc.flux = lc.flux - np.average(lc.flux)

        for i in lcs[1:]:
            i = i.PDCSAP_FLUX.remove_nans()
            i.flux = -2.5 * np.log10(i.flux)
            i.flux = i.flux - np.average(i.flux)
            lc = lc.append(i)
            
        return TimeDelay(lc.time, lc.flux, **kwargs)

    def time_delay(self, segment_size=10):
        """ Calculates the time delay signal, splitting the lightcurve into 
        chunks of width segment_size """
        uHz_conv = 1e-6 * 24 * 60 * 60  # Factor to convert between day^-1 and uHz
        times, mags = self.times, self.mags

        time_0 = times[0]
        time_slice, mag_slice, mid_time, phase = [], [], [], []
        self.time_delays, self.time_midpoints = [], []

        # Loop over lightcurve
        for t, y  in tqdm(zip(times, mags), total=len(times)):
            time_slice.append(t)
            mag_slice.append(y)
            
            # In each segment
            if t - time_0 > segment_size:
                # Append the time midpoint
                self.time_midpoints.append(np.mean(time_slice))
                
                # And the phases for each frequency
                phase.append(self.dft_phase(time_slice, mag_slice, self.nu))
                time_0 = t
                time_slice, mag_slice = [], []
                
        phase = np.array(phase).T
        for ph, f in zip(phase, self.nu):
            # Phase wrapping patch
            mean_phase = np.mean(ph)
            ph[np.where(ph - mean_phase > np.pi/2)] -= np.pi
            ph[np.where(ph - mean_phase < -np.pi/2)] += np.pi
            
            ph -= np.mean(ph)
            td = ph / (2*np.pi*(f / uHz_conv * 1e-6))
            self.time_delays.append(td)
        return self.time_midpoints, self.time_delays

    def dft_phase(self, x, y, freq, verbose=False):
        freq = np.asarray(freq)
        if freq.ndim == 0:
            freq = freq[None]
        
        x = np.array(x)
        y = np.array(y)
        phase = []
        for f in freq:
            expo = 2.0 * np.pi * f * x
            ft_real = np.sum(y * np.cos(expo))
            ft_imag = np.sum(y * np.sin(expo))
            phase.append(np.arctan(ft_imag/ft_real))
        return phase

    def plot_td(self, periodogram=True, **kwargs):

        time_midpoints, time_delays = self.time_delay(**kwargs)
        colors = ['red','darkorange','gold','seagreen','dodgerblue','darkorchid','mediumvioletred']

        if periodogram:
            fig, ax = plt.subplots(2,1,figsize=[10,10])
            periodogram_freq, periodogram_amp = self.periodogram()
            ax[1].plot(periodogram_freq, periodogram_amp, "k", linewidth=0.5)
            ax[1].set_xlabel("frequency [cpd]")
            ax[1].set_ylabel("Amplitude [mag]")
            for freq, color in zip(self.nu, colors):
                ax[1].scatter(freq, np.max(periodogram_amp), c=color)
        else:
            fig, ax = plt.subplots(figsize=[10,5])
            ax = [ax]
        for delay, color in zip(time_delays, colors):
            ax[0].scatter(time_midpoints,delay, alpha=1, s=8,c=color)
            ax[0].set_xlabel('Time [BJD]')
            ax[0].set_ylabel(r'Time delay $\tau$ [s]')
        return ax

    def estimate_frequencies(self, max_peaks=7, oversample=4.0, tflow=True):

        """ This function does some fancy peak fitting to estimate the main
        frequencies of the lightcurve. """
        x = self.times
        y = self.mags

        tmax = x.max()
        tmin = x.min()
        dt = np.median(np.diff(x))
        df = 1.0 / (tmax - tmin)
        ny = 0.5 / dt

        freq = np.arange(df, 2 * ny, df / oversample)
        power = LombScargle(x, y).power(freq)

        # Find peaks
        peak_inds = (power[1:-1] > power[:-2]) & (power[1:-1] > power[2:])
        peak_inds = np.arange(1, len(power)-1)[peak_inds]
        peak_inds = peak_inds[np.argsort(power[peak_inds])][::-1]
        peaks = []
        for j in range(max_peaks):
            i = peak_inds[0]
            freq0 = freq[i]
            alias = 2.0*ny - freq0

            m = np.abs(freq[peak_inds] - alias) > 25*df
            m &= np.abs(freq[peak_inds] - freq0) > 25*df

            peak_inds = peak_inds[m]
            peaks.append(freq0)
        
        if tflow:
            try:
                import tensorflow as tf
            except ImportError:
                raise ImportError('tensorflow package is required for this')
            # Optimize the model
            T = tf.float64
            t = tf.constant(x, dtype=T)
            f = tf.constant(y, dtype=T)
            nu = tf.Variable(peaks, dtype=T)
            arg = 2*np.pi*nu[None, :]*t[:, None]
            D = tf.concat([tf.cos(arg), tf.sin(arg),
                        tf.ones((len(x), 1), dtype=T)],
                        axis=1)

            # Solve for the amplitudes and phases of the oscillations
            DTD = tf.matmul(D, D, transpose_a=True)
            DTy = tf.matmul(D, f[:, None], transpose_a=True)
            w = tf.linalg.solve(DTD, DTy)
            model = tf.squeeze(tf.matmul(D, w))
            chi2 = tf.reduce_sum(tf.square(f - model))

            opt = tf.contrib.opt.ScipyOptimizerInterface(chi2, [nu],
                                                        method="L-BFGS-B")
            with tf.Session() as sess:
                sess.run(nu.initializer)
                opt.minimize(sess)
                return sess.run(nu)
        else:
            return np.array(peaks)

    def periodogram(self, oversample=2., samples=100000):
        """ Calculates the periodogram of the lightcurve """
        t = self.times
        y = self.mags

        uHz_conv = 1e-6 * 24 * 60 * 60  # Factor to convert between day^-1 and uHz

        nyquist = 0.5 / np.median(np.diff(t))
        nyquist = nyquist / uHz_conv
        freq_uHz = np.linspace(1e-2, nyquist * oversample, samples)
        freq = freq_uHz * uHz_conv

        model = LombScargle(t, y)
        power = model.power(freq, method="fast", normalization="psd")

        # Convert to amplitude
        fct = np.sqrt(4./len(t))
        amp = np.sqrt(np.abs(power)) * fct
        return freq_uHz * uHz_conv, amp

    def plot_periodogram(self, ax=None, **kwargs):
        """ Plots the periodogram of the lightcurve """
        per_freq, per_amp = self.periodogram(**kwargs)
        if ax is None:
            fig, ax = plt.subplots()
        ax.plot(per_freq, per_amp, "k", linewidth=0.5)
        ax.set_xlabel("Frequency [cpd]")
        ax.set_ylabel("Amplitude [mag]")
        ax.set_xlim(per_freq[0], per_freq[-1])
        ax.set_xlabel(r"frequency $[d^{-1}]$")
        nyquist = 0.5 / np.median(np.diff(self.times))
        ax.axvline(nyquist, c='r')
        ax.set_ylabel("Amplitude")
        ax.set_ylim([0,None])
        return ax

    def first_look(self, segment_size=5):
        fig, ax = plt.subplots(4,1,figsize=[12,12])

        t, y = self.times, self.mags

        # Lightcurve
        ax[0].plot(t, y, "k", linewidth=0.5)
        ax[0].set_xlabel('Time [BJD]')
        ax[0].set_ylabel('Magnitude [mag]')
        ax[0].set_xlim([t.min(), t.max()])
        ax[0].invert_yaxis()

        # Periodogram
        periodogram_freq, periodogram_amp = self.periodogram()
        ax[1].plot(periodogram_freq, periodogram_amp, "k", linewidth=0.5)
        ax[1].set_xlabel("Frequency [$d^{-1}$]")
        ax[1].set_ylabel("Amplitude [mag]")
        colors = ['red','darkorange','gold','seagreen','dodgerblue','darkorchid','mediumvioletred']
        for freq, color in zip(self.nu, colors):
                ax[1].scatter(freq, np.max(periodogram_amp), c=color)
        ax[1].set_xlim([periodogram_freq[0], periodogram_freq[-1]])
        ax[1].set_ylim([0,None])

        # Time delays
        time_midpoints, time_delays = self.time_delay(segment_size)
        for delay, color in zip(time_delays, colors):
            ax[2].scatter(time_midpoints,delay, alpha=1, s=8,c=color)
            ax[2].set_xlabel('Time [BJD]')
            ax[2].set_ylabel(r'$\tau [s]$')
        ax[2].set_xlim([t.min(), t.max()])

        # Averaged periodogram
        t = time_midpoints
        y = np.average(time_delays, axis=0)

        nyquist = 0.5 / np.median(np.diff(t))
        freq = np.linspace(1e-2, nyquist, 100000)

        model = LombScargle(t, y)
        sc = model.power(freq, method="fast", normalization="psd")
        amp = np.sqrt(np.abs(sc)) * np.sqrt(4./len(t))
        ax[3].plot(freq, amp, "k", linewidth=0.5)
        ax[3].set_xlabel("Frequency $[d^{-1}]$")
        ax[3].set_ylabel(r'Average $\tau$ amplitude')
        ax[3].set_xlim([freq[0], freq[-1]])
        ax[3].set_ylim([0,None])

        plt.subplots_adjust(hspace=0.33)
        plt.show()