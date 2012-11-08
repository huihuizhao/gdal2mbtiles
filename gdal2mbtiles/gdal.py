# -*- coding: utf-8 -*-

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import errno
from math import pi
from itertools import count
import os
import re
from subprocess import CalledProcessError, check_output, Popen, PIPE
from tempfile import NamedTemporaryFile
from xml.etree import ElementTree

import numpy

from osgeo import gdal, gdalconst, osr
from osgeo.gdalconst import (GA_ReadOnly, GRA_Bilinear, GRA_Cubic,
                             GRA_CubicSpline, GRA_Lanczos,
                             GRA_NearestNeighbour)

gdal.UseExceptions()            # Make GDAL throw exceptions on error
osr.UseExceptions()             # And OSR as well.


from .constants import (EPSG_WEB_MERCATOR, GDALTRANSLATE,
                        GDALWARP, TILE_SIDE)
from .exceptions import (GdalError, CalledGdalError, UnalignedInputError,
                         UnknownResamplingMethodError)
from .types import Extents, GdalFormat, XY


RESAMPLING_METHODS = {
    GRA_NearestNeighbour: 'near',
    GRA_Bilinear: 'bilinear',
    GRA_Cubic: 'cubic',
    GRA_CubicSpline: 'cubicspline',
    GRA_Lanczos: 'lanczos',
}


def check_output_gdal(*popenargs, **kwargs):
    p = Popen(stderr=PIPE, stdout=PIPE, *popenargs, **kwargs)
    stdoutdata, stderrdata = p.communicate()
    if p.returncode:
        cmd = kwargs.get("args")
        if cmd is None:
            cmd = popenargs[0]
        raise CalledGdalError(p.returncode, cmd, output=stdoutdata,
                              error=stderrdata.rstrip('\n'))
    return stdoutdata


def preprocess(inputfile, outputfile, band=None, spatial_ref=None,
               resampling=None, compress=None, **kwargs):
    functions = []

    # Extract desired band to reduce the amount of warping
    if band is not None:
        functions.append(lambda f: extract_color_band(inputfile=f, band=band))

    # Warp
    functions.append(lambda f: warp(inputfile=f, spatial_ref=spatial_ref,
                                    resampling=resampling)),

    return pipeline(inputfile=inputfile, outputfile=outputfile,
                    functions=functions, compress=compress, **kwargs)


def pipeline(inputfile, outputfile, functions, **kwargs):
    """
    Applies VRT-functions to a GDAL-readable inputfile, rendering outputfile.

    Functions must be an iterable of single-parameter functions that take a
    filename as input.
    """
    if not functions:
        raise ValueError('Must have at least one function')

    tmpfiles = []
    try:
        previous = inputfile
        for i, f in enumerate(functions):
            vrt = f(previous)
            current = vrt.get_tempfile(suffix='.vrt', prefix=('gdal%d' % i))
            tmpfiles.append(current)
            previous = current.name
        return vrt.render(outputfile=outputfile, **kwargs)
    finally:
        for f in tmpfiles:
            f.close()


def extract_color_band(inputfile, band):
    """
    Takes an inputfile (probably a VRT) and generates a single-band VRT.
    """
    dataset = Dataset(inputfile)
    if not 1 <= band <= dataset.RasterCount:
        raise ValueError(
            "band must be between 1 and {0}".format(dataset.RasterCount)
        )

    command = [
        GDALTRANSLATE,
        '-q',                   # Quiet
        '-of', 'VRT',           # Output to VRT
        '-b', band,             # Single band
        inputfile,
        '/dev/stdout'
    ]
    try:
        return VRT(check_output_gdal([str(e) for e in command]))
    except CalledGdalError as e:
        if e.error == ("ERROR 4: `/dev/stdout' not recognised as a supported "
                       "file format."):
            # HACK: WTF?!?
            return VRT(e.output)
        raise


def warp(inputfile, spatial_ref=None, cmd=GDALWARP, resampling=None,
         maximum_resolution=None):
    """
    Takes an GDAL-readable inputfile and generates the VRT to warp it.
    """
    dataset = Dataset(inputfile)

    warp_cmd = [
        cmd,
        '-q',                   # Quiet - FIXME: Use logging
        '-of', 'VRT',           # Output to VRT
    ]

    # Warping to Mercator.
    #
    # Note that EPSG:3857 replaces this EPSG:3785 but GDAL doesn't know about
    # it yet.
    if spatial_ref is None:
        spatial_ref = SpatialReference.FromEPSG(EPSG_WEB_MERCATOR)
    warp_cmd.extend(['-t_srs', spatial_ref.GetEPSGString()])

    # Resampling method
    if resampling is not None:
        if not isinstance(resampling, basestring):
            try:
                resampling = RESAMPLING_METHODS[resampling]
            except KeyError:
                raise UnknownResamplingMethodError(resampling)
        elif resampling not in RESAMPLING_METHODS.values():
            raise UnknownResamplingMethodError(resampling)
        warp_cmd.extend(['-r', resampling])

    # Compute the target extents
    src_spatial_ref = dataset.GetSpatialReference()
    transform = CoordinateTransformation(src_spatial_ref, spatial_ref)
    resolution = dataset.GetNativeResolution(transform=transform,
                                             maximum=maximum_resolution)
    extents = dataset.GetTiledExtents(transform=transform,
                                             resolution=resolution)
    warp_cmd.append('-te')
    warp_cmd.extend(map(
        # Ensure that we use as much precision as possible for floating point
        # numbers.
        '{!r}'.format,
        [
            extents.lower_left.x, extents.lower_left.y,   # xmin ymin
            extents.upper_right.x, extents.upper_right.y  # xmax ymax
        ]
    ))

    # Generate an output file with an whole number of tiles, in pixels.
    num_tiles = spatial_ref.GetTilesCount(extents=extents,
                                          resolution=resolution)
    warp_cmd.extend([
        '-ts',
        int(num_tiles.x) * TILE_SIDE,
        int(num_tiles.y) * TILE_SIDE
    ])

    # Propagate No Data Value
    nodata_values = [dataset.GetRasterBand(i).GetNoDataValue()
                     for i in range(1, dataset.RasterCount + 1)]
    if any(nodata_values):
        nodata_values = [str(v).lower() for v in nodata_values]
        warp_cmd.extend(['-dstnodata', ' '.join(nodata_values)])

    # Call gdalwarp
    warp_cmd.extend([inputfile, '/dev/stdout'])
    return VRT(check_output_gdal([str(e) for e in warp_cmd]))


def supported_formats(cmd=GDALWARP):
    if supported_formats._cache is None:
        result = None
        output = check_output([cmd, '--formats'])
        for line in output.splitlines():
            # Look for the header
            if result is None:
                if line == 'Supported Formats:':
                    result = []
                continue

            m = supported_formats.format_re.match(line)
            if m:
                attributes = frozenset(m.group('attributes'))
                result.append(GdalFormat(can_read=('r' in attributes),
                                         can_write=('w' in attributes),
                                         can_update=('+' in attributes),
                                         has_virtual_io=('v' in attributes),
                                         **m.groupdict()))

        supported_formats._cache = result

    return supported_formats._cache
supported_formats.format_re = re.compile(r'\s+(?P<name>.+?)'
                                         r'\s+\((?P<attributes>.+?)\):'
                                         r'\s+(?P<description>.*)$')
supported_formats._cache = None


def resampling_methods(cmd=GDALWARP):
    if resampling_methods._cache is None:
        result = None
        try:
            output = check_output([cmd, '--help'])
        except CalledProcessError as e:
            if e.returncode == 1 and e.output is not None:
                output = e.output
            else:
                raise

        for line in output.splitlines():
            # Look for the header
            if result is None:
                if line == 'Available resampling methods:':
                    result = []
                continue

            result.extend(m.strip(' \t.').split()[0] for m in line.split(','))
            if line.endswith('.'):
                break

        resampling_methods._cache = result

    return resampling_methods._cache
resampling_methods._cache = None


# Utility classes that wrap GDAL because its SWIG bindings are not Pythonic.

class Band(gdal.Band):
    """
    Wrapper class for gdal.Band

    band: gdal.Band object retrieved from gdal.Dataset
    dataset: gdal.Dataset object that is the parent of `band`
    """

    def __init__(self, band, dataset):
        # Since this is a SWIG object, clone the ``this`` pointer
        self.this = band.this
        # gdal.Dataset deletes all of its data structures when it is deleted.
        self._dataset = dataset

    def GetMetadataItem(self, name, domain=''):
        """Wrapper around gdal.Band.GetMetadataItem()"""
        return super(Band, self).GetMetadataItem(bytes(name), bytes(domain))

    def GetNoDataValue(self):
        """Returns gdal.Band.GetNoDataValue() as a NumPy type"""
        result = super(Band, self).GetNoDataValue()
        if result is not None:
            return self.NumPyDataType(result)

    @property
    def NumPyDataType(self):
        """Returns the NumPy type associated with gdal.Band.DataType"""
        datatype = self.DataType
        if datatype == gdalconst.GDT_Byte:
            pixeltype = self.GetMetadataItem('PIXELTYPE', 'IMAGE_STRUCTURE')
            if pixeltype == 'SIGNEDBYTE':
                return numpy.int8
            return numpy.uint8
        elif datatype == gdalconst.GDT_UInt16:
            return numpy.uint16
        elif datatype == gdalconst.GDT_UInt32:
            return numpy.uint32
        elif datatype == gdalconst.GDT_Int16:
            return numpy.int16
        elif datatype == gdalconst.GDT_Int32:
            return numpy.int32
        elif datatype == gdalconst.GDT_Float32:
            return numpy.float32
        elif datatype == gdalconst.GDT_Float64:
            return numpy.float64
        else:
            raise ValueError(
                "Cannot handle DataType: {0}".format(
                    gdal.GetDataTypeName(datatype)
                )
            )

    @property
    def MinimumValue(self):
        """Returns the minimum value that can be stored in this band"""
        datatype = self.NumPyDataType
        if issubclass(datatype, numpy.integer):
            return numpy.iinfo(datatype).min
        elif issubclass(datatype, numpy.floating):
            return -numpy.inf
        else:
            raise TypeError("Cannot handle DataType: {0}".format(datatype))

    @property
    def MaximumValue(self):
        """Returns the minimum value that can be stored in this band"""
        datatype = self.NumPyDataType
        if issubclass(datatype, numpy.integer):
            return numpy.iinfo(datatype).max
        elif issubclass(datatype, numpy.floating):
            return numpy.inf
        else:
            raise TypeError("Cannot handle DataType: {0}".format(datatype))

    def IncrementValue(self, value):
        """Returns the next `value` expressible in this band"""
        datatype = self.NumPyDataType
        if issubclass(datatype, numpy.integer):
            if not isinstance(value, (int, long, numpy.integer)):
                raise TypeError(
                    'value {0!r} must be compatible with {1}'.format(
                        value, datatype.__name__
                    )
                )
            iinfo = numpy.iinfo(datatype)
            minint, maxint = iinfo.min, iinfo.max
            if not minint <= value <= maxint:
                raise ValueError(
                    'value {0!r} must be between {1} and {2}'.format(
                        value, minint, maxint
                    )
                )
            if value == maxint:
                return maxint
            return value + 1

        elif issubclass(datatype, numpy.floating):
            if not isinstance(value, (int, long, numpy.integer,
                                      float, numpy.floating)):
                raise TypeError(
                    "value {0!r} must be compatible with {1}".format(
                        value, datatype.__name__
                    )
                )
            if value == numpy.finfo(datatype).max:
                return numpy.inf
            return numpy.nextafter(datatype(value), datatype(numpy.inf))

        else:
            raise TypeError("Cannot handle DataType: {0}".format(datatype))


class CoordinateTransformation(osr.CoordinateTransformation):
    def __init__(self, src_ref, dst_ref):
        # GDAL doesn't give us access to the source and destination
        # SpatialReferences, so we save them in the object.
        self.src_ref = src_ref
        self.dst_ref = dst_ref

        super(CoordinateTransformation, self).__init__(self.src_ref,
                                                       self.dst_ref)


class Dataset(gdal.Dataset):
    def __init__(self, inputfile, mode=GA_ReadOnly):
        """
        Opens a GDAL-readable file.

        Raises a GdalError if inputfile is invalid.
        """
        # Open the input file and read some metadata
        open(inputfile, 'r').close()  # HACK: GDAL gives a useless exception

        if isinstance(inputfile, unicode):
            inputfile = inputfile.encode('utf-8')
        try:
            # Since this is a SWIG object, clone the ``this`` pointer
            self.this = gdal.Open(inputfile, mode).this
        except RuntimeError as e:
            raise GdalError(e.message)

    def GetRasterBand(self, i):
        return Band(band=super(Dataset, self).GetRasterBand(i),
                    dataset=self)

    def GetSpatialReference(self):
        return SpatialReference(self.GetProjection())

    def GetCoordinateTransformation(self, dst_ref):
        return CoordinateTransformation(src_ref=self.GetSpatialReference(),
                                        dst_ref=dst_ref)

    def GetNativeResolution(self, transform=None, maximum=None):
        """
        Get a native destination resolution that does not reduce the precision
        of the source data.
        """
        # Get the source projection's units for a 1x1 pixel
        _, width, _, _, _, height = self.GetGeoTransform()
        src_pixel_size = min(abs(width), abs(height))

        if transform is None:
            dst_pixel_size = src_pixel_size
            dst_ref = self.GetSpatialReference()
        else:
            # Transform these dimensions into the destination projection
            dst_pixel_size = transform.TransformPoint(src_pixel_size, 0)[0]
            dst_pixel_size = abs(dst_pixel_size)
            dst_ref = transform.dst_ref

        # We allow some floating point error between src_pixel_size and
        # dst_pixel_size
        error = dst_pixel_size * 1.0e-06

        # Find the resolution where the pixels are smaller than dst_pixel_size.
        for resolution in count():
            if maximum is not None and resolution >= maximum:
                return resolution

            res_pixel_size = max(
                *dst_ref.GetPixelDimensions(resolution=resolution)
            )
            if (res_pixel_size - dst_pixel_size) <= error:
                return resolution

    def PixelCoordinates(self, x, y, transform=None):
        """
        Transforms pixel co-ordinates into the destination projection.

        If transform is None, no reprojection occurs and the dataset's
        SpatialReference is used.
        """
        # Assert that pixel_x and pixel_y are valid
        if not 0 <= x <= self.RasterXSize:
            raise ValueError('x %r is not between 0 and %d' %
                             (x, self.RasterXSize))
        if not 0 <= y <= self.RasterYSize:
            raise ValueError('y %r is not between 0 and %d' %
                             (y, self.RasterYSize))

        geotransform = self.GetGeoTransform()
        coords = XY(
            geotransform[0] + geotransform[1] * x + geotransform[2] * y,
            geotransform[3] + geotransform[4] * x + geotransform[5] * y
        )

        if transform is None:
            return coords

        # Reproject
        return XY(*transform.TransformPoint(coords.x, coords.y)[0:2])

    def GetExtents(self, transform=None):
        """
        Returns (lower-left, upper-right) extents in transform's destination
        projection.

        If transform is None, no reprojection occurs and the dataset's
        SpatialReference is used.
        """
        # Prepare GDAL functions to compute extents
        x_size, y_size = self.RasterXSize, self.RasterYSize

        # Compute four corners in destination projection
        upper_left = self.PixelCoordinates(0, 0,
                                           transform=transform)
        upper_right = self.PixelCoordinates(x_size, 0,
                                            transform=transform)
        lower_left = self.PixelCoordinates(0, y_size,
                                           transform=transform)
        lower_right = self.PixelCoordinates(x_size, y_size,
                                            transform=transform)
        x_values, y_values = zip(upper_left, upper_right,
                                 lower_left, lower_right)

        # Return lower-left and upper-right extents
        return Extents(lower_left=XY(min(x_values), min(y_values)),
                       upper_right=XY(max(x_values), max(y_values)))

    def GetTiledExtents(self, transform=None, resolution=None):
        if resolution is None:
            resolution = self.GetNativeResolution(transform=transform)

        # Get the tile dimensions in map units
        if transform is None:
            spatial_ref = self.GetSpatialReference()
        else:
            spatial_ref = transform.dst_ref
        tile_width, tile_height = spatial_ref.GetTileDimensions(
            resolution=resolution
        )

        # Project the extents to the destination projection.
        extents = self.GetExtents(transform=transform)

        # Correct for origin, because you can't do modular arithmetic on
        # half-tiles.
        left, bottom = spatial_ref.OffsetPoint(*extents.lower_left)
        right, top = spatial_ref.OffsetPoint(*extents.upper_right)

        # Compute the extents aligned to the above tiles.
        left -= left % tile_width
        right += -right % tile_width
        bottom -= bottom % tile_height
        top += -top % tile_height

        # Undo the correction.
        left, bottom = spatial_ref.OffsetPoint(left, bottom, reverse=True)
        right, top = spatial_ref.OffsetPoint(right, top, reverse=True)

        # Ensure that the extents within the boundaries of the destination
        # projection.
        world_extents = spatial_ref.GetWorldExtents()
        left = max(left, world_extents.lower_left.x)
        bottom = max(bottom, world_extents.lower_left.y)
        right = min(right, world_extents.upper_right.x)
        top = min(top, world_extents.upper_right.y)

        # Undo the correction.
        return Extents(lower_left=XY(left, bottom),
                       upper_right=XY(right, top))

    def GetTmsExtents(self, resolution=None, transform=None):
        """
        Returns (lower-left, upper-right) TMS tile coordinates.

        The upper-right coordinates are excluded from the range, while the
        lower-left are included.
        """
        if resolution is None:
            resolution = self.GetNativeResolution()

        if transform is None:
            spatial_ref = self.GetSpatialReference()
        else:
            spatial_ref = transform.dst_ref

        tile_width, tile_height = spatial_ref.GetTileDimensions(
            resolution=resolution
        )

        # Validate that the native resolution extents are tile-aligned.
        extents = self.GetTiledExtents(transform=transform)
        if not extents.almost_equal(self.GetExtents(transform=transform),
                                    places=2):
            raise UnalignedInputError('Dataset is not aligned to TMS grid')

        # Correct for origin, because you can't do modular arithmetic on
        # half-tiles.
        extents = self.GetTiledExtents(transform=transform,
                                       resolution=resolution)
        left, bottom = spatial_ref.OffsetPoint(*extents.lower_left)
        right, top = spatial_ref.OffsetPoint(*extents.upper_right)

        # Divide by number of tiles
        return Extents(lower_left=XY(int(left / tile_width),
                                     int(bottom / tile_height)),
                       upper_right=XY(int(right / tile_width),
                                      int(top / tile_height)))

    def GetWorldTmsExtents(self, resolution=None, transform=None):
        if resolution is None:
            resolution = self.GetNativeResolution()

        if transform is None:
            spatial_ref = self.GetSpatialReference()
        else:
            spatial_ref = transform.dst_ref

        world_tiles = spatial_ref.GetTilesCount(
            extents=spatial_ref.GetWorldExtents(),
            resolution=resolution
        )
        return Extents(lower_left=XY(0, 0),
                       upper_right=world_tiles)

    def GetWorldTmsBorders(self, resolution=None, transform=None):
        """Returns an iterable of TMS tiles that are outside this Dataset."""
        world_extents = self.GetWorldTmsExtents(resolution=resolution,
                                                transform=transform)
        data_extents = self.GetTmsExtents(resolution=resolution,
                                          transform=transform)
        return (XY(x, y)
                for x in xrange(world_extents.lower_left.x,
                                world_extents.upper_right.x)
                for y in xrange(world_extents.lower_left.y,
                                world_extents.upper_right.y)
                if XY(x, y) not in data_extents)


class SpatialReference(osr.SpatialReference):
    def __init__(self, *args, **kwargs):
        super(SpatialReference, self).__init__(*args, **kwargs)
        self._angular_transform = None

    @classmethod
    def FromEPSG(cls, code):
        s = cls()
        s.ImportFromEPSG(code)
        return s

    def __eq__(self, other):
        return bool(self.IsSame(other))

    def GetEPSGCode(self):
        epsg_string = self.GetEPSGString()
        if epsg_string:
            return int(epsg_string.split(':')[1])

    def GetEPSGString(self):
        if self.IsLocal() == 1:
            return

        if self.IsGeographic() == 1:
            cstype = b'GEOGCS'
        else:
            cstype = b'PROJCS'
        return '{0}:{1}'.format(self.GetAuthorityName(cstype),
                                self.GetAuthorityCode(cstype))

    def GetMajorCircumference(self):
        if self.IsProjected() == 0:
            return 2 * pi / self.GetAngularUnits()
        return self.GetSemiMajor() * 2 * pi / self.GetLinearUnits()

    def GetMinorCircumference(self):
        if self.IsProjected() == 0:
            return 2 * pi / self.GetAngularUnits()
        return self.GetSemiMinor() * 2 * pi / self.GetLinearUnits()

    def GetWorldExtents(self):
        major = self.GetMajorCircumference() / 2
        minor = self.GetMinorCircumference() / 2
        if self.IsProjected() == 0:
            minor /= 2
        return Extents(lower_left=XY(-major, -minor),
                       upper_right=XY(major, minor))

    def OffsetPoint(self, x, y, reverse=False):
        major_offset = self.GetMajorCircumference() / 2
        minor_offset = self.GetMinorCircumference() / 2
        if self.IsProjected() == 0:
            # The semi-minor-axis is only off by 1/4 of the world
            minor_offset = self.GetMinorCircumference() / 4

        if reverse:
            major_offset = -major_offset
            minor_offset = -minor_offset

        return XY(x + major_offset,
                  y + minor_offset)

    def GetPixelDimensions(self, resolution):
        # Assume square pixels.
        width, height = self.GetTileDimensions(resolution=resolution)
        return XY(width / TILE_SIDE,
                  height / TILE_SIDE)

    def GetTileDimensions(self, resolution):
        # Assume square tiles.
        width = self.GetMajorCircumference() / 2 ** resolution
        height = self.GetMinorCircumference() / 2 ** resolution
        if self.IsProjected() == 0:
            # Resolution 0 only covers a longitudinal hemisphere
            return XY(width / 2, height / 2)
        else:
            # Resolution 0 covers the whole world
            return XY(width, height)

    def GetTilesCount(self, extents, resolution):
        width = extents.upper_right.x - extents.lower_left.x
        height = extents.upper_right.y - extents.lower_left.y

        tile_width, tile_height = self.GetTileDimensions(resolution=resolution)

        return XY(int(round(width / tile_width)),
                  int(round(height / tile_height)))


class VRT(object):
    def __init__(self, content):
        self.content = content

    def __str__(self):
        return self.content

    def get_root(self):
        return ElementTree.fromstring(self.content)

    def get_tempfile(self, **kwargs):
        kwargs.setdefault('suffix', '.vrt')
        tempfile = NamedTemporaryFile(**kwargs)
        tempfile.write(self.content)
        tempfile.flush()
        tempfile.seek(0)
        return tempfile

    def render(self, outputfile, cmd=GDALWARP, working_memory=512,
               compress=None, tempdir=None):
        """Generate a GeoTIFF from a vrt string"""
        tmpfile = NamedTemporaryFile(
            suffix='.tif', prefix='gdalrender',
            dir=os.path.dirname(outputfile), delete=False
        )

        try:
            with self.get_tempfile(dir=tempdir) as inputfile:
                warp_cmd = [
                    cmd,
                    '-q',                   # Quiet - FIXME: Use logging
                    '-of', 'GTiff',         # Output to GeoTIFF
                    '-multi',               # Use multiple processes
                    '-overwrite',           # Overwrite outputfile
                    '-co', 'BIGTIFF=IF_NEEDED',  # Use BigTIFF if needed
                    '-wo', 'NUM_THREADS=ALL_CPUS',  # Use all CPUs
                ]

                # Set the working memory so that gdalwarp doesn't stall of disk
                # I/O
                warp_cmd.extend([
                    '-wm', working_memory,
                    '--config', 'GDAL_CACHEMAX', working_memory
                ])

                # Use compression
                compress = str(compress).upper()
                if compress and compress != 'NONE':
                    warp_cmd.extend(['-co', 'COMPRESS=%s' % compress])
                    if compress in ('LZW', 'DEFLATE'):
                        warp_cmd.extend(['-co', 'PREDICTOR=2'])

                # Run gdalwarp and output to tmpfile.name
                warp_cmd.extend([inputfile.name, tmpfile.name])
                check_output_gdal([str(e) for e in warp_cmd])

                # If it succeeds, then we move it to overwrite the actual
                # output
                os.rename(tmpfile.name, outputfile)
        finally:
            try:
                os.remove(tmpfile.name)
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise
