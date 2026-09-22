# -*- coding: utf-8 -*-
"""
Layer(s) to GeoPackage
=============================================

QGIS Processing script. Exports several layers loaded in the project
(vector, non-spatial tables, raster) together into a single target
GeoPackage.

Behavior:
- Vector layers are written as standalone layers/tables into the
  GeoPackage (no warning).
- Non-spatial tables (vector layers without geometry) are also
  supported, but trigger a warning.
- Raster layers are written as a tiled raster table into the
  GeoPackage, trigger a warning, and can be compressed
  (PNG / PNG8 / JPEG / WEBP).
- Without a target CRS, each layer keeps its original CRS (a
  GeoPackage can contain tables with different CRSs).
- With a target CRS set, all layers are transformed to it; for
  rasters, the resampling method can then be chosen.
- Mesh, point cloud, vector tile, annotation and plugin layers are
  not supported by this tool and are skipped when running (with an
  error message in the log), since they cannot be meaningfully
  written to a GeoPackage.
- Optionally, the successfully exported layers are added back to the
  project afterwards, collected in a layer tree group named after
  the GeoPackage, each keeping its original layer name.

Installation:
1. Save this file somewhere (e.g. as layers_to_geopackage.py).
2. In QGIS: Processing -> Toolbox -> click the script icon at the top
   of the toolbox -> "Add Script to Toolbox..." -> select this file.
   Alternatively, place the file directly in the profile folder
   ".../QGIS3/profiles/default/processing/scripts/"; QGIS reads that
   folder automatically on startup.
3. The tool then appears under "LiberGIS" in the Processing
   toolbox.
"""

import os
import re
import time
import contextlib

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingUtils,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterDefinition,
    QgsVectorLayer,
    QgsRasterLayer,
    QgsWkbTypes,
    QgsVectorFileWriter,
    QgsCoordinateTransform,
    QgsProject,
)

try:
    from osgeo import gdal
    gdal.UseExceptions()
    GDAL_AVAILABLE = True
except ImportError:
    GDAL_AVAILABLE = False


class LayersToGeoPackage(QgsProcessingAlgorithm):

    INPUT_LAYERS = 'INPUT_LAYERS'
    OUTPUT_GPKG = 'OUTPUT_GPKG'
    TARGET_CRS = 'TARGET_CRS'
    RESAMPLING = 'RESAMPLING'
    TILE_FORMAT = 'TILE_FORMAT'
    QUALITY = 'QUALITY'
    OVERWRITE = 'OVERWRITE'
    LOAD_LAYERS = 'LOAD_LAYERS'

    # Display name -> gdal.Warp(resampleAlg=...) value
    RESAMPLE_METHODS = [
        ('Nearest neighbour (categorical/discrete data)', 'near'),
        ('Bilinear', 'bilinear'),
        ('Cubic', 'cubic'),
        ('Cubic spline', 'cubicspline'),
        ('Lanczos', 'lanczos'),
        ('Average (mean)', 'average'),
        ('Mode (most frequent value, categorical data)', 'mode'),
        ('Maximum', 'max'),
        ('Minimum', 'min'),
        ('Median', 'med'),
    ]

    # Display name -> GDAL GPKG creation option TILE_FORMAT
    TILE_FORMATS = [
        ('Automatic (GDAL default: PNG for transparency, otherwise JPEG)', ''),
        ('PNG (lossless)', 'PNG'),
        ('PNG8 (256-color palette, compact, slightly lossy)', 'PNG8'),
        ('JPEG (lossy, no transparency)', 'JPEG'),
        ('WEBP (lossy, with transparency)', 'WEBP'),
    ]

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return LayersToGeoPackage()

    def name(self):
        return 'layerstogeopackage'

    def displayName(self):
        return self.tr('Layer(s) to GeoPackage')

    def group(self):
        return self.tr('LiberGIS')

    def groupId(self):
        return 'libergis'

    def tags(self):
        return [
            self.tr('geopackage'), self.tr('gpkg'), self.tr('export'),
            self.tr('package'), self.tr('multiple layers'),
            self.tr('batch'), self.tr('vector'), self.tr('raster'),
            self.tr('merge'), self.tr('combine'), self.tr('compression'),
            self.tr('compress'), self.tr('group'), self.tr('layer tree'),
        ]

    def shortHelpString(self):
        return self.tr(
            'Exports several selected project layers (vector, tables, '
            'raster) together into a target GeoPackage.\n\n'
            '- Vector layers are saved as standalone layers in the GeoPackage.\n'
            '- Tables without geometry are supported, but trigger a warning.\n'
            '- Raster layers are saved as a tile table (warning) and can be '
            'compressed (PNG/PNG8/JPEG/WEBP).\n'
            '- Without a target CRS, each layer keeps its original CRS.\n'
            '- With a target CRS set, all layers are transformed; for '
            'rasters, the resampling method can then be chosen.\n'
            '- Mesh, point cloud, vector tile, annotation and plugin '
            'layers are not supported and are skipped.\n'
            '- Optionally, exported layers are added back to the project '
            'afterwards in a layer tree group named after the GeoPackage.'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_LAYERS,
                self.tr('Layers to export'),
                layerType=QgsProcessing.TypeMapLayer
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT_GPKG,
                self.tr('Target GeoPackage'),
                fileFilter=self.tr('GeoPackage (*.gpkg)')
            )
        )
        self.addParameter(
            QgsProcessingParameterCrs(
                self.TARGET_CRS,
                self.tr(
                    "Target CRS (leave empty to keep each layer's "
                    'original CRS)'
                ),
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.RESAMPLING,
                self.tr(
                    'Resampling method for rasters (only relevant if a '
                    'target CRS is set and rasters are reprojected)'
                ),
                options=[m[0] for m in self.RESAMPLE_METHODS],
                defaultValue=2
            )
        )

        tile_format_param = QgsProcessingParameterEnum(
            self.TILE_FORMAT,
            self.tr('Tile format / compression for raster layers'),
            options=[t[0] for t in self.TILE_FORMATS],
            defaultValue=0
        )
        tile_format_param.setFlags(
            tile_format_param.flags() | QgsProcessingParameterDefinition.FlagAdvanced
        )
        self.addParameter(tile_format_param)

        quality_param = QgsProcessingParameterNumber(
            self.QUALITY,
            self.tr('Quality for JPEG/WEBP (1-100)'),
            type=QgsProcessingParameterNumber.Integer,
            minValue=1,
            maxValue=100,
            defaultValue=75
        )
        quality_param.setFlags(
            quality_param.flags() | QgsProcessingParameterDefinition.FlagAdvanced
        )
        self.addParameter(quality_param)

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.OVERWRITE,
                self.tr(
                    'Overwrite existing target GeoPackage (instead of '
                    'adding/replacing layers within it)'
                ),
                defaultValue=False
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.LOAD_LAYERS,
                self.tr(
                    'Add exported layers to the project, grouped in a '
                    'layer tree group named after the GeoPackage'
                ),
                defaultValue=True
            )
        )

    # ---------------------------------------------------------------
    # Helper functions
    # ---------------------------------------------------------------

    @staticmethod
    def _sanitize_name(name):
        """Builds a valid GeoPackage table name from the layer name."""
        sanitized = re.sub(r'[^A-Za-z0-9_]', '_', name.strip())
        if not sanitized:
            sanitized = 'layer'
        if sanitized[0].isdigit():
            sanitized = '_' + sanitized
        return sanitized[:60]

    def _build_unique_names(self, layers):
        """Assigns unique table names to all selected layers."""
        used = set()
        names = {}
        for layer in layers:
            base = self._sanitize_name(layer.name())
            candidate = base
            counter = 1
            while candidate.lower() in used:
                counter += 1
                candidate = f'{base}_{counter}'
            used.add(candidate.lower())
            names[layer.id()] = candidate
        return names

    @staticmethod
    def _remove_existing_gpkg(path, feedback, attempts=5, delay=0.3):
        """Deletes an existing destination file, including SQLite side
        files. Retries on failure: on Windows, a file that was only just
        closed (e.g. a GeoPackage that was still loaded as a project
        layer a moment ago) can briefly stay locked by the OS/another
        process - the first delete attempt then fails with
        PermissionError (WinError 32) even though nothing is actually
        wrong. A short retry loop resolves this in almost all cases
        instead of failing outright on the first try."""
        for suffix in ('', '-wal', '-shm', '-journal'):
            candidate = path + suffix
            if not os.path.exists(candidate):
                continue
            last_exc = None
            for attempt in range(attempts):
                try:
                    os.remove(candidate)
                    last_exc = None
                    break
                except OSError as exc:
                    last_exc = exc
                    time.sleep(delay)
            if last_exc is not None:
                feedback.reportError(
                    f"Could not delete existing file '{candidate}' after "
                    f"{attempts} attempts: {last_exc}"
                )

    @staticmethod
    def _gdal_cancel_callback(feedback):
        """GDAL progress callback that immediately aborts a running
        gdal.Warp/Translate call as soon as feedback.isCanceled() is True -
        otherwise clicking 'Cancel' would only take effect AFTER the
        current raster finishes, which can take a long time for large
        files. Returns 0 = abort, 1 = continue (GDAL's convention for
        progress callbacks)."""
        def _cb(complete, message, user_data):
            return 0 if feedback.isCanceled() else 1
        return _cb

    @staticmethod
    @contextlib.contextmanager
    def _gdal_warning_capture():
        """Captures GDAL/CPL warning-and-above messages emitted while the
        wrapped block runs, instead of relying solely on
        gdal.UseExceptions(). UseExceptions() only converts failure-level
        errors into Python exceptions - a warning-level message (GDAL
        silently falling back to a different setting, dropping an
        unsupported creation option, etc.) stays invisible otherwise,
        which could mask a raster table that was written but not quite as
        intended. Debug-level chatter is filtered out to avoid noise.
        Yields a list that gets populated with (err_class, err_no,
        err_msg) tuples."""
        messages = []

        def _handler(err_class, err_no, err_msg):
            if err_class >= gdal.CE_Warning:
                messages.append((err_class, err_no, err_msg))

        gdal.PushErrorHandler(_handler)
        try:
            yield messages
        finally:
            gdal.PopErrorHandler()

    def _write_vector_layer(self, layer, output_path, layer_name, target_crs,
                             transform_context, is_first_write, feedback):
        try:
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = 'GPKG'
            options.layerName = layer_name
            options.actionOnExistingFile = (
                QgsVectorFileWriter.CreateOrOverwriteFile if is_first_write
                else QgsVectorFileWriter.CreateOrOverwriteLayer
            )
            # Lets QGIS cancel writing in the middle of a large layer
            # table, instead of only between layers in the outer loop.
            options.feedback = feedback
            try:
                options.fileEncoding = layer.dataProvider().encoding() or 'UTF-8'
            except Exception:
                options.fileEncoding = 'UTF-8'

            if target_crs.isValid() and layer.crs() != target_crs:
                original_extent = layer.extent()
                options.ct = QgsCoordinateTransform(layer.crs(), target_crs, transform_context)
                transformed_extent = options.ct.transformBoundingBox(original_extent)

                # A transform that silently failed (e.g. a missing PROJ
                # grid) typically leaves the coordinates untouched rather
                # than raising an error. Two different, valid CRSs
                # "coincidentally" producing a bit-identical bounding box
                # is not realistically going to happen with real data, so
                # this is a reliable check rather than something to eyeball.
                unchanged = (
                    abs(transformed_extent.xMinimum() - original_extent.xMinimum()) < 1e-9
                    and abs(transformed_extent.yMinimum() - original_extent.yMinimum()) < 1e-9
                    and abs(transformed_extent.xMaximum() - original_extent.xMaximum()) < 1e-9
                    and abs(transformed_extent.yMaximum() - original_extent.yMaximum()) < 1e-9
                )

                feedback.pushInfo(
                    f"Layer '{layer.name()}': reprojecting from "
                    f"{layer.crs().authid() or 'unknown CRS'} "
                    f"({original_extent.toString(1)}) to "
                    f"{target_crs.authid()} ({transformed_extent.toString(1)})."
                )
                if unchanged:
                    feedback.pushWarning(
                        f"Layer '{layer.name()}': the extent is identical "
                        'before and after reprojection, even though the '
                        'source and target CRS differ. This usually means '
                        'the transform silently failed - check for missing '
                        'PROJ transformation grids (Settings > Options > '
                        'CRS Handling).'
                    )

            result = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, output_path, transform_context, options
            )
        except Exception as exc:
            if feedback.isCanceled():
                feedback.pushInfo(f"Vector layer '{layer.name()}': processing canceled.")
            else:
                feedback.reportError(
                    f"Unexpected error writing vector layer '{layer.name()}' "
                    f"as '{layer_name}': {exc}"
                )
            return False

        # Return value is a tuple (WriterError, error message, ...) - unpack defensively
        error_code = result[0]
        error_message = result[1] if len(result) > 1 else ''

        if error_code != QgsVectorFileWriter.NoError:
            if feedback.isCanceled():
                feedback.pushInfo(f"Vector layer '{layer.name()}': processing canceled.")
            else:
                feedback.reportError(
                    f"Error writing vector layer '{layer.name()}' "
                    f"as '{layer_name}': {error_message}"
                )
            return False
        return True

    def _write_raster_layer(self, layer, output_path, layer_name, target_crs,
                             resample_alg, tile_format, quality, feedback):
        if not GDAL_AVAILABLE:
            feedback.reportError(
                'GDAL Python bindings (osgeo.gdal) are not available - '
                f"raster layer '{layer.name()}' cannot be written."
            )
            return False

        if layer.providerType() != 'gdal':
            feedback.reportError(
                f"Raster layer '{layer.name()}' uses the data provider "
                f"'{layer.providerType()}' (e.g. WMS/WMTS/XYZ) and cannot "
                'be written to a GeoPackage by this tool - skipped.'
            )
            return False

        source_path = layer.source().split('|')[0]

        # Check data type: the GeoPackage tile mechanism is designed for
        # 8-bit (Byte) image data. For continuous values (e.g. Float32
        # elevation models), a separate GeoTIFF is usually the better
        # choice - we warn specifically in this case without blocking
        # the export.
        try:
            probe_ds = gdal.Open(source_path)
        except Exception:
            probe_ds = None
        if probe_ds is not None:
            band_count = probe_ds.RasterCount
            is_byte = (
                band_count > 0
                and probe_ds.GetRasterBand(1).DataType == gdal.GDT_Byte
            )
            probe_ds = None
            if not is_byte:
                feedback.pushWarning(
                    f"Layer '{layer.name()}' does not have an 8-bit data "
                    'type (e.g. an elevation model or analytical raster '
                    'with floating-point/16-bit values). The GeoPackage '
                    'tile mechanism is designed for 8-bit image data; '
                    'lossy formats (JPEG/WEBP/PNG8) are unsuitable here '
                    'and even lossless PNG can alter the values. For '
                    'such data, a separate GeoTIFF is usually the better '
                    'choice - be sure to check the result.'
                )

        creation_options = ['APPEND_SUBDATASET=YES', f'RASTER_TABLE={layer_name}']
        if tile_format:
            creation_options.append(f'TILE_FORMAT={tile_format}')
        if tile_format in ('JPEG', 'WEBP'):
            creation_options.append(f'QUALITY={quality}')
        elif tile_format in ('', 'PNG', 'PNG8'):
            # PNG's own internal compression is DEFLATE-based (ZLEVEL
            # 1-9). Level 9 was tested and rejected as too slow for the
            # marginal size gain; level 6 is set explicitly here as the
            # chosen speed/size tradeoff rather than relying on whatever
            # a given GDAL build happens to default to.
            creation_options.append('ZLEVEL=6')

        needs_reprojection = target_crs.isValid() and layer.crs() != target_crs

        # Diagnostic logging: the GDAL raster path is the least
        # thoroughly tested part of this script (vector export via
        # QgsVectorFileWriter is a very well-established QGIS core
        # feature; the interplay of APPEND_SUBDATASET/RASTER_TABLE/
        # TILE_FORMAT when writing repeatedly into the same GPKG file is
        # less so). If a first test run fails here, the exact GDAL
        # options are in the log so it can be diagnosed precisely
        # instead of just seeing "error".
        feedback.pushInfo(
            f"Layer '{layer.name()}': GDAL creation options {creation_options}; "
            + (
                f"reprojecting to {target_crs.authid()} (resampling: {resample_alg})"
                if needs_reprojection else 'no reprojection (original CRS is kept)'
            )
        )

        gdal_messages = []
        try:
            with self._gdal_warning_capture() as gdal_messages:
                if needs_reprojection:
                    warp_options = gdal.WarpOptions(
                        format='GPKG',
                        dstSRS=target_crs.toWkt(),
                        resampleAlg=resample_alg,
                        creationOptions=creation_options,
                        multithread=True,
                        callback=self._gdal_cancel_callback(feedback),
                    )
                    result_ds = gdal.Warp(output_path, source_path, options=warp_options)
                else:
                    translate_options = gdal.TranslateOptions(
                        format='GPKG',
                        creationOptions=creation_options,
                        callback=self._gdal_cancel_callback(feedback),
                    )
                    result_ds = gdal.Translate(output_path, source_path, options=translate_options)
        except Exception as exc:
            for _, _, msg in gdal_messages:
                feedback.pushWarning(f"GDAL message for layer '{layer.name()}': {msg}")
            if feedback.isCanceled():
                feedback.pushInfo(f"Raster layer '{layer.name()}': processing canceled.")
            else:
                feedback.reportError(
                    f"Error writing raster layer '{layer.name()}' "
                    f"as '{layer_name}': {exc}"
                )
            return False

        # Surface any GDAL warnings even on a technically "successful" call -
        # gdal.UseExceptions() would not have raised for these, but they can
        # indicate the output is not quite what was expected (see docstring
        # of _gdal_warning_capture).
        for _, _, msg in gdal_messages:
            feedback.pushWarning(f"GDAL message for layer '{layer.name()}': {msg}")

        if result_ds is None:
            if feedback.isCanceled():
                feedback.pushInfo(f"Raster layer '{layer.name()}': processing canceled.")
            else:
                feedback.reportError(
                    f"Raster layer '{layer.name()}' could not be written "
                    'to the GeoPackage.'
                )
            return False

        result_ds = None  # close dataset / flush cache
        return True

    def _queue_layer_for_loading(self, context, layer, output_path, layer_name, group_name):
        """Registers a successfully written GeoPackage table to be loaded
        back into the project once the whole algorithm finishes, placed in
        a layer tree group named after the GeoPackage.

        Deliberately uses context.addLayerToLoadOnCompletion() rather than
        calling QgsProject.instance().addMapLayer() directly:
        processAlgorithm() can run in a background thread, and touching
        the project/layer tree directly from a worker thread is not
        thread-safe. addLayerToLoadOnCompletion() just records what to
        load; the Processing framework itself performs the actual loading
        on the main thread after the algorithm returns, which is the
        supported mechanism for exactly this "one algorithm run produces
        several layers" case.
        """
        if isinstance(layer, QgsVectorLayer):
            uri = f'{output_path}|layername={layer_name}'
            type_hint = QgsProcessingUtils.LayerHint.Vector
        else:
            uri = f'GPKG:{output_path}:{layer_name}'
            type_hint = QgsProcessingUtils.LayerHint.Raster

        details = QgsProcessingContext.LayerDetails(
            layer.name(), context.project(), self.OUTPUT_GPKG, type_hint
        )
        # Use the layer's original (friendly) name rather than the
        # sanitized GeoPackage table name, and force QGIS to actually use
        # it regardless of the user's local Processing "layer naming"
        # setting.
        details.forceName = True
        details.groupName = group_name
        context.addLayerToLoadOnCompletion(uri, details)

    # ---------------------------------------------------------------
    # Main entry point
    # ---------------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_LAYERS, context)
        if not layers:
            raise QgsProcessingException(self.tr('No layer was selected.'))

        output_path = self.parameterAsFileOutput(parameters, self.OUTPUT_GPKG, context)
        if not output_path.lower().endswith('.gpkg'):
            output_path += '.gpkg'

        target_crs = self.parameterAsCrs(parameters, self.TARGET_CRS, context)
        resample_index = self.parameterAsEnum(parameters, self.RESAMPLING, context)
        resample_alg = self.RESAMPLE_METHODS[resample_index][1]
        tile_format_index = self.parameterAsEnum(parameters, self.TILE_FORMAT, context)
        tile_format = self.TILE_FORMATS[tile_format_index][1]
        quality = self.parameterAsInt(parameters, self.QUALITY, context)
        overwrite = self.parameterAsBoolean(parameters, self.OVERWRITE, context)
        load_layers = self.parameterAsBoolean(parameters, self.LOAD_LAYERS, context)
        group_name = os.path.splitext(os.path.basename(output_path))[0]

        if overwrite:
            self._remove_existing_gpkg(output_path, feedback)
        else:
            out_dir = os.path.dirname(output_path)
            if out_dir and not os.path.exists(out_dir):
                os.makedirs(out_dir, exist_ok=True)

        layer_names = self._build_unique_names(layers)
        transform_context = QgsProject.instance().transformContext()

        any_written = False
        exported, warned, failed = [], [], []

        total = len(layers)
        for index, layer in enumerate(layers):
            if feedback.isCanceled():
                break
            feedback.setProgress(int(index / total * 100))
            layer_name = layer_names[layer.id()]
            feedback.pushInfo(f"Processing layer '{layer.name()}' -> table '{layer_name}' ...")

            if isinstance(layer, QgsVectorLayer):
                is_table = (
                    not layer.isSpatial()
                    or layer.geometryType() == QgsWkbTypes.NullGeometry
                )
                if is_table:
                    feedback.pushWarning(
                        f"Layer '{layer.name()}' is a table without geometry. "
                        'It will be saved as a standalone (non-spatial) '
                        'attribute table in the GeoPackage.'
                    )
                    warned.append(layer.name())

                success = self._write_vector_layer(
                    layer, output_path, layer_name, target_crs,
                    transform_context, not any_written, feedback
                )

            elif isinstance(layer, QgsRasterLayer):
                feedback.pushWarning(
                    f"Layer '{layer.name()}' is a raster layer. Rasters are "
                    'saved as a tiled table in the GeoPackage - depending '
                    'on the chosen tile format, this may involve lossy '
                    'compression.'
                )
                warned.append(layer.name())
                success = self._write_raster_layer(
                    layer, output_path, layer_name, target_crs,
                    resample_alg, tile_format, quality, feedback
                )

            else:
                feedback.reportError(
                    f"Layer '{layer.name()}' has a type not supported by "
                    'this tool (e.g. mesh, point cloud, vector tile, '
                    'annotation, or plugin layer) and will be skipped.'
                )
                failed.append(layer.name())
                continue

            if success:
                any_written = True
                exported.append(layer.name())
                if load_layers:
                    self._queue_layer_for_loading(
                        context, layer, output_path, layer_name, group_name
                    )
            else:
                failed.append(layer.name())

        feedback.pushInfo('---')
        feedback.pushInfo(f'Successfully exported: {len(exported)} of {total} layer(s).')
        if warned:
            feedback.pushInfo(f"Exported with a warning: {', '.join(warned)}")
        if failed:
            feedback.pushInfo(f"Not exported (see messages above): {', '.join(failed)}")

        return {self.OUTPUT_GPKG: output_path}
