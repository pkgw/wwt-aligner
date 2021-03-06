# Copyright 2020-2021 the .NET Foundation
# Licensed under the MIT License

"""
Main tool driver.
"""

__all__ = [
    'go',
    'plot_fits_sources',
    'plot_index',
]

from astropy.io import fits
from astropy.table import Table
from astropy.wcs import WCS
from dataclasses import dataclass
import math
import numpy as np
import os.path
from PIL import Image as pil_image
from pyavm import AVM
import sep
import subprocess
import sys
from toasty.builder import Builder
from toasty.image import ImageLoader

from . import logger

def image_size_to_anet_preset(size_deg):
    """
    Get an astrometry.net "preset" size from an image size. Docs say:

    0 => ~6 arcmin image width
    2 => ~12 arcmin
    4 => ~24 arcmin

    etc. Our "size_deg" parameter is the diagonal size of the image,
    which offsets the calculation by one step or so. The minimum
    allowed preset is -5.
    """
    return max(-5, int(np.floor(5 + 2 * math.log2(size_deg))))


@dataclass
class FitsInfo(object):
    large_scale_deg: float = 0.0
    width_deg: float = 0.0
    width_pixels: int = 0
    height_pixels: int = 0
    bgsub_data: np.array = None
    sep_objects: np.array = None
    wcs_objects: Table = None

@dataclass
class ExtractionConfig(object):
    bg_box_size: int = 32
    bg_filter_size: int = 3
    bg_filter_threshold: float = 0.0
    source_threshold: int = 10

def source_extract_fits(
    fits_path,
    log_prefix='',
):
    info = FitsInfo()
    config = ExtractionConfig()

    # Figure out the HDU to use, and read data + WCS

    data = None

    with fits.open(fits_path) as hdul:
        for hdu_num, hdu in enumerate(hdul):
            logger.debug('%sconsidering HDU #%d; data shape %r', log_prefix, hdu_num, hdu.shape)

            if hdu.data is None:
                continue  # reject: no data

            if hasattr(hdu, 'columns'):
                continue  # reject: tabular

            if len(hdu.shape) < 2:
                continue  # reject: not at least 2D

            # OK, it looks like this the HDU we want!
            wcs = WCS(hdu)
            data = hdu.data
            height, width = data.shape[-2:]
            break

    assert data is not None, 'failed to find a usable image HDU'
    data = data.byteswap(inplace=True).newbyteorder()

    # Get some useful housekeeping data

    info.width_pixels = width
    info.height_pixels = height

    midx = width // 2
    midy = height // 2
    coords = wcs.pixel_to_world(
        [midx, midx + 1, 0, width, 0, width],
        [midy, midy + 1, 0, height, midy, midy],
    )

    info.large_scale_deg = coords[2].separation(coords[3]).deg
    logger.debug('%slarge scale for this image: %e deg', log_prefix, info.large_scale_deg)
    info.width_deg = coords[4].separation(coords[5]).deg
    logger.debug('%scharacteristic width for this image: %e deg', log_prefix, info.width_deg)

    # Use SEP to find sources

    logger.info('%sFinding sources ...', log_prefix)

    bkg = sep.Background(
        data,
        bw = config.bg_box_size,
        bh = config.bg_box_size,
        fw = config.bg_filter_size,
        fh = config.bg_filter_size,
        fthresh = config.bg_filter_threshold,
    )
    logger.debug('%sSEP background level: %e', log_prefix, bkg.globalback)
    logger.debug('%sSEP background rms: %e', log_prefix, bkg.globalrms)
    bkg.subfrom(data)
    info.bgsub_data = data

    info.sep_objects = sep.extract(data, config.source_threshold, err=bkg.globalrms)
    logger.debug('%sSEP object count: %d', log_prefix, len(info.sep_objects))

    coords = wcs.pixel_to_world(info.sep_objects['x'], info.sep_objects['y'])
    info.wcs_objects = Table(
        [coords.ra.deg, coords.dec.deg, info.sep_objects['flux']],
        names=('RA', 'DEC', 'FLUX')
    )

    # All done!
    return info


def plot_fits_sources(fits_path):
    import matplotlib.pyplot as plt
    from matplotlib.patches import Ellipse

    try:
        info = source_extract_fits(fits_path)
    except Exception as e:
        raise Exception(f'could not extract sources from `{fits_path}`') from e

    # Get the figure to have about as many pixels as the image

    from matplotlib import rcParams
    DPI = 72
    rcParams['figure.figsize'] = [info.width_pixels / DPI, info.height_pixels / DPI]

    # This stuff derived from the SEP documentation.

    fig, ax = plt.subplots()
    m = np.mean(info.bgsub_data)
    s = np.std(info.bgsub_data)
    im = ax.imshow(
        info.bgsub_data,
        interpolation = 'nearest',
        cmap = 'gray',
        vmin = m - s,
        vmax = m + s,
        origin='lower',
    )

    for i in range(len(info.sep_objects)):
        e = Ellipse(
            xy = (info.sep_objects['x'][i], info.sep_objects['y'][i]),
            width = 6 * info.sep_objects['a'][i],
            height = 6 * info.sep_objects['b'][i],
            angle = info.sep_objects['theta'][i] * 180. / np.pi,
        )
        e.set_facecolor('none')
        e.set_edgecolor('red')
        ax.add_artist(e)

    fig.tight_layout()
    img_path = os.path.splitext(fits_path)[0] + '_sources.png'
    fig.savefig(img_path)
    logger.debug('saved sources image to `%s`', img_path)


def index_extracted_image(
    objects_fits,
    index_fits,
    index_log = None,
    extraction_info = None,
    index_unique_key = None,
    anet_bin_prefix = '',
    log_prefix = '',
):
    if not index_log:
        raise ValueError('index_log')
    if not extraction_info:
        raise ValueError('extraction_info')
    if not index_unique_key:
        raise ValueError('index_unique_key')

    argv = [
        anet_bin_prefix + 'build-astrometry-index',
        '-i', objects_fits,
        '-o', index_fits,
        '-I', index_unique_key,
        '-E',  # objects table is much less than all-sky
        '-f',  # our sort column is flux-like, not mag-like
        '-S', 'FLUX',
        '-P', str(image_size_to_anet_preset(extraction_info.large_scale_deg)),
    ]
    logger.debug('%sindex command: %s', log_prefix, ' '.join(argv))

    logger.info('%sGenerating Astrometry.Net index ...', log_prefix)

    with open(index_log, 'wb') as log:
        subprocess.check_call(
            argv,
            stdout = log,
            stderr = subprocess.STDOUT,
            shell = False,
        )


def plot_index(
    fits_path,
    anet_bin_prefix = '',
):
    from astrometry.plot.plotstuff import Plotstuff
    import shutil
    import tempfile

    # Sourcefind ...

    try:
        info = source_extract_fits(fits_path)
    except Exception as e:
        raise Exception(f'could not extract sources from `{fits_path}`') from e

    work_dir = tempfile.mkdtemp()
    objects_fits = os.path.join(work_dir, f'objects.fits')
    info.wcs_objects.write(objects_fits, format='fits', overwrite=True)
    index_fits = os.path.join(work_dir, f'index.fits')
    index_log = os.path.join(work_dir, f'build-index.log')

    # Index ...

    try:
        index_extracted_image(
            objects_fits,
            index_fits,
            index_log = index_log,
            extraction_info = info,
            index_unique_key = '0',
            anet_bin_prefix = anet_bin_prefix,
        )
    except Exception as e:
        shutil.rmtree(work_dir)
        raise Exception(f'could not index the sources from `{fits_path}') from e

    # Plot!

    with fits.open(fits_path) as hdul:
        hdu = hdul[0]  # haack
        wcs = WCS(hdu.header)
        height, width = hdu.shape[-2:]
        midx = width // 2
        midy = height // 2
        coords = wcs.pixel_to_world(
            [midx, 0, width],
            [midy, 0, height],
        )
        rdw = (coords[0].ra.deg, coords[0].dec.deg, coords[1].separation(coords[2]).deg)

    plot = Plotstuff(outformat='png', size=(800, 800), rdw=rdw)
    ind = plot.index
    plot.color = 'white'
    ind.add_file(index_fits)
    ind.stars = True
    ind.quads = True
    ind.fill = False
    plot.plot('index')
    ind.stars = False
    ind.fill = True
    plot.alpha = 0.1
    plot.plot('index')
    plot.alpha = 0.5
    plot.plot_grid(0.02, 0.02, 0.02, 0.02)

    img_path = os.path.splitext(fits_path)[0] + '_index.png'
    plot.write(img_path)
    logger.debug('saved index image to `%s`', img_path)


@dataclass
class AlignmentConfig(object):
    scale_range_factor: float = 2.0
    """How to set the ANet scale-low and scale-high parameters, based on the
    widths of the reference FITS images."""

    solve_time_limit_seconds: int = 30
    "The CPU time limit for the solver, in seconds."

    downsample_factor: int = 2
    "How much to downsample the source RGB image for sourcefinding"

    object_limit: int = 1000
    "Limit the number of objects processed from the RGB image"

def go(
    fits_paths = None,
    rgb_path = None,
    output_path = None,
    tile_path = None,
    work_dir = '',
    anet_bin_prefix = '',
):
    """
    Do the whole thing.
    """
    cfg = AlignmentConfig()
    index_fits_list = []
    scale_low = scale_high = None

    for fits_num, fits_path in enumerate(fits_paths):
        logger.info('Processing reference science image `%s` ...', fits_path)

        try:
            info = source_extract_fits(fits_path, log_prefix='  ')
        except Exception as e:
            logger.warning('  Failed to extract sources from this file')
            logger.warning('  Caused by: %s', e)
            continue

        objects_fits = os.path.join(work_dir, f'objects{fits_num}.fits')
        info.wcs_objects.write(objects_fits, format='fits', overwrite=True)

        # Generate the Astrometry.Net index

        index_fits = os.path.join(work_dir, f'index{fits_num}.fits')
        index_log = os.path.join(work_dir, f'build-index-{fits_num}.log')

        try:
            index_extracted_image(
                objects_fits,
                index_fits,
                index_log = index_log,
                extraction_info = info,
                index_unique_key = str(fits_num),
                anet_bin_prefix = anet_bin_prefix,
                log_prefix = '  ',
            )
        except Exception as e:
            logger.warning('  Failed to index this file')
            logger.warning('  Caused by: %s', e)
            continue

        # Success!

        index_fits_list.append(index_fits)

        this_scale_low = info.width_deg * 60 / cfg.scale_range_factor  # units are image width in arcmin
        this_scale_high = info.width_deg * 60 * cfg.scale_range_factor

        if scale_low is None:
            scale_low = this_scale_low
            scale_high = this_scale_high
        else:
            scale_low = min(scale_low, this_scale_low)
            scale_high = max(scale_high, this_scale_high)

    if not index_fits_list:
        raise Exception('cannot align: failed to index any of the input FITS files')

    # Write out config file

    cfg_path = os.path.join(work_dir, 'aligner.cfg')

    with open(cfg_path, 'wt') as f:
        print('add_path', work_dir, file=f)
        print('inparallel', file=f)

        for p in index_fits_list:
            print('index', p, file=f)

    # Solve our input image

    wcs_file = os.path.join(work_dir, 'solved.fits')

    # https://manpages.debian.org/testing/astrometry.net/solve-field.1.en.html
    argv = [
        anet_bin_prefix + 'solve-field',
        '-v',
        '--config', cfg_path,
        '--scale-units', 'arcminwidth',
        '--scale-low', str(scale_low),
        '--scale-high', str(scale_high),
        '--cpulimit', str(cfg.solve_time_limit_seconds),
        '--objs', str(cfg.object_limit),
        '--dir', work_dir,
        '-N', wcs_file,
        '--no-plots',
        '--no-tweak',
        '--downsample', str(cfg.downsample_factor),
        rgb_path,
    ]
    logger.debug('solve command: %s', ' '.join(argv))

    solve_log = os.path.join(work_dir, 'solve-field.log')
    logger.info('Launching Astrometry.Net solver for `%s` ...', rgb_path)

    try:
        with open(solve_log, 'wb') as log:
            subprocess.check_call(
                argv,
                stdout = log,
                stderr = subprocess.STDOUT,
                shell = False,
            )

        assert os.path.exists(wcs_file), 'Astrometry.Net did not emit a solution file'
    except Exception as e:
        logger.error('  Failed to solve this image')
        logger.error('  Proximate Python exception: %s', e)
        logger.error('  Output from solve-field:')

        try:
            with open(solve_log, 'r') as f:
                for line in f:
                    logger.error('    %s', line.rstrip())
        except Exception as sub_e:
            logger.error('     [failed to read the log! error: %s]', sub_e)

        raise

    # Convert solution to AVM, with hardcoded parity inversion.
    #
    # TODO: map positive parity into both AVM and WWT metadata correctly.

    img = ImageLoader().load_path(rgb_path)

    with fits.open(wcs_file) as hdul:
        header = hdul[0].header
        wcs = WCS(header)

    hdwork = wcs.to_header()
    hdwork['CRPIX2'] = img.height + 1 - hdwork['CRPIX2']
    hdwork['PC1_2'] *= -1
    hdwork['PC2_2'] *= -1
    wcs = WCS(hdwork)
    avm = AVM.from_wcs(wcs, shape=(img.height, img.width))

    # Apply AVM
    #
    # pyavm can't convert image formats, so if we've been asked to emit a tagged
    # imagine in a format different than the image format, we need to do that
    # conversion manually. We're not in a great position to be clever so we
    # assess "format" from filename extensions.

    in_name_pieces = os.path.splitext(os.path.basename(rgb_path))

    if output_path is None:
        output_path = in_name_pieces[0] + '_tagged' + in_name_pieces[1]

    input_ext = in_name_pieces[1].lower()
    output_ext = os.path.splitext(output_path)[1].lower()

    if input_ext != output_ext:
        logger.info('Converting input image to create `%s`', output_path)
        img.save(output_path, format=output_ext.replace('.', ''))

        logger.info('Adding AVM tags to `%s`', output_path)
        avm.embed(output_path, output_path)
    else:
        logger.info('Writing AVM-tagged image to:`%s`', output_path)
        avm.embed(rgb_path, output_path)

    # Tile it for WWT, if requested

    if tile_path is not None:
        from toasty.merge import averaging_merger, cascade_images
        from toasty.pyramid import PyramidIO

        logger.info('Creating base layer of WWT tiling ...')

        pio = PyramidIO(tile_path, default_format=img.default_format)
        builder = Builder(pio)
        builder.make_thumbnail_from_other(img)
        builder.tile_base_as_study(img, cli_progress=True)
        builder.apply_wcs_info(wcs, img.width, img.height)
        builder.set_name(in_name_pieces[0])
        builder.write_index_rel_wtml()

        logger.info('Cascading tiles ...')
        cascade_images(
            pio,
            builder.imgset.tile_levels,
            averaging_merger,
            cli_progress=True
        )
