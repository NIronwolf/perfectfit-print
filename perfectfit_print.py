#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gi

gi.require_version("Gimp", "3.0")
from gi.repository import Gimp

gi.require_version("GimpUi", "3.0")
from gi.repository import GimpUi
from gi.repository import GObject
from gi.repository import GLib
from gi.repository import Gtk
from gi.repository import Gdk, GdkPixbuf  # Import Gdk

import sys

plug_in_proc = "plug-in-perfectfit-print"
plug_in_binary = "perfectfit-print"


def _get_base_thumbnail(image):
    """
    Creates a high-resolution base thumbnail of the image, scaled to fit
    within a 1024x1024 box.
    """
    if not image or image.get_width() == 0 or image.get_height() == 0:
        return None

    img_width = image.get_width()
    img_height = image.get_height()
    img_aspect = img_width / img_height

    # Calculate dimensions to fit within a 1024x1024 box
    if img_aspect > 1:  # Landscape
        target_width = 1024
        target_height = int(1024 / img_aspect)
    else:  # Portrait or Square
        target_height = 1024
        target_width = int(1024 * img_aspect)
    
    target_width = max(1, target_width)
    target_height = max(1, target_height)

    return image.get_thumbnail(target_width, target_height, True)


def _get_zoomed_view(base_thumbnail, x_scale, y_scale, x_offset, y_offset):
    """
    Takes a base thumbnail and slider values, and returns a new pixbuf
    containing the panned-and-zoomed view.
    """
    if not base_thumbnail or (x_scale == 1.0 and y_scale == 1.0):
        return base_thumbnail

    thumb_w = base_thumbnail.get_width()
    thumb_h = base_thumbnail.get_height()

    zoomed_pixbuf = GdkPixbuf.Pixbuf.new(
        GdkPixbuf.Colorspace.RGB,
        base_thumbnail.get_has_alpha(),
        base_thumbnail.get_bits_per_sample(),
        thumb_w,
        thumb_h,
    )
    if base_thumbnail.get_has_alpha():
        zoomed_pixbuf.fill(0x00000000)

    # Corrected LOGIC based on negative offsets
    # 1. Calculate the centering offset to keep the zoom centered
    center_offset_x = -((thumb_w * x_scale - thumb_w) / 2)
    center_offset_y = -((thumb_h * y_scale - thumb_h) / 2)

    # 2. Calculate the panning offset from the slider
    pan_range_x = thumb_w * x_scale - thumb_w
    pan_range_y = thumb_h * y_scale - thumb_h
    pan_offset_x = x_offset * pan_range_x
    pan_offset_y = y_offset * pan_range_y

    # 3. Final offset is the sum
    final_offset_x = center_offset_x + pan_offset_x
    final_offset_y = center_offset_y + pan_offset_y

    base_thumbnail.scale(
        dest=zoomed_pixbuf,
        dest_x=0,
        dest_y=0,
        dest_width=thumb_w,
        dest_height=thumb_h,
        offset_x=final_offset_x,
        offset_y=final_offset_y,
        scale_x=x_scale,
        scale_y=y_scale,
        interp_type=GdkPixbuf.InterpType.BILINEAR,
    )

    return zoomed_pixbuf


def _draw_overlays(cr, config, thumb_w, thumb_h, dest_x, dest_y):
    """
    Draws the dimming overlay and the dashed crop rectangle.
    """
    target_w_prop = config.get_property("width")
    target_h_prop = config.get_property("height")

    if target_h_prop <= 0:
        return

    target_aspect = target_w_prop / target_h_prop
    thumb_aspect = thumb_w / thumb_h

    if target_aspect > thumb_aspect:
        crop_w = thumb_w
        crop_h = int(crop_w / target_aspect)
    else:
        crop_h = thumb_h
        crop_w = int(crop_h * target_aspect)

    x_slop = thumb_w - crop_w
    y_slop = thumb_h - crop_h

    crop_x = dest_x + x_slop / 2
    crop_y = dest_y + y_slop / 2

    cr.set_source_rgba(0, 0, 0, 0.5)
    cr.rectangle(dest_x, dest_y, thumb_w, crop_y - dest_y)
    cr.rectangle(
        dest_x, crop_y + crop_h, thumb_w, (dest_y + thumb_h) - (crop_y + crop_h)
    )
    cr.rectangle(dest_x, crop_y, crop_x - dest_x, crop_h)
    cr.rectangle(
        crop_x + crop_w, crop_y, (dest_x + thumb_w) - (crop_x + crop_w), crop_h
    )
    cr.fill()

    cr.set_line_width(1.0)
    cr.rectangle(crop_x + 0.5, crop_y + 0.5, crop_w - 1, crop_h - 1)
    cr.set_dash([4, 4])
    cr.set_source_rgb(0, 0, 0)
    cr.stroke_preserve()
    cr.set_dash([4, 4], 4)
    cr.set_source_rgb(1, 1, 1)
    cr.stroke()
    cr.set_dash([])


def perfectfit_print_run(procedure, run_mode, image, drawables, config, data):
    if run_mode == Gimp.RunMode.INTERACTIVE:
        GimpUi.init(plug_in_binary)

        dialog = GimpUi.ProcedureDialog.new(procedure, config, "PerfectFit Print")

        # --- Main UI Construction ---
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        dialog.get_content_area().add(main_vbox)

        # Keep the top settings grid as the user has configured it
        settings_grid = Gtk.Grid()
        settings_grid.set_column_spacing(12)
        settings_grid.set_row_spacing(6)
        settings_grid.set_border_width(6)
        main_vbox.pack_start(settings_grid, False, False, 0)

        # Width (as per user's code)
        label = Gtk.Label.new_with_mnemonic("_Width:")
        settings_grid.attach(label, 0, 0, 1, 1)
        w_width = GimpUi.prop_spin_button_new(config, "width", 0.05, 1.0, 2)
        label.set_mnemonic_widget(w_width)
        settings_grid.attach(w_width, 1, 0, 1, 1)

        # Height (as per user's code)
        label = Gtk.Label.new_with_mnemonic("_Height:")
        settings_grid.attach(label, 2, 0, 1, 1)
        w_height = GimpUi.prop_spin_button_new(config, "height", 0.05, 1.0, 2)
        label.set_mnemonic_widget(w_height)
        settings_grid.attach(w_height, 3, 0, 1, 1)

        # Unit (as per user's code)
        label = Gtk.Label.new_with_mnemonic("_Unit:")
        settings_grid.attach(label, 4, 0, 1, 1)
        w_unit = GimpUi.prop_unit_combo_box_new(config, "unit")
        label.set_mnemonic_widget(w_unit)
        settings_grid.attach(w_unit, 5, 0, 1, 1)

        # Middle preview and scrollbars using a Gtk.Grid
        # Grid layout:
        # [  ][x_offset][  ]
        # [y_offset][preview][x_scale]
        # [  ][y_scale][lock ]
        preview_grid = Gtk.Grid()
        preview_grid.set_column_spacing(6)
        preview_grid.set_row_spacing(6)
        preview_grid.set_border_width(6)
        main_vbox.pack_start(preview_grid, True, True, 0)

        # X-Offset Scrollbar (Top, Horizontal)
        adj_x_offset = Gtk.Adjustment(
            value=config.get_property("x_offset"),
            lower=-0.5,
            upper=0.5,
            step_increment=0.001,
            page_increment=0.01,
        )
        w_x_offset = Gtk.Scrollbar(
            orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj_x_offset
        )
        config.bind_property(
            "x_offset", adj_x_offset, "value", GObject.BindingFlags.DEFAULT
        )
        preview_grid.attach(w_x_offset, 1, 0, 1, 1)  # Column 1, Row 0
        w_x_offset.set_hexpand(True)
        w_x_offset.set_vexpand(False)

        # Y-Offset Scrollbar (Left, Vertical)
        adj_y_offset = Gtk.Adjustment(
            value=config.get_property("y_offset"),
            lower=-0.5,
            upper=0.5,
            step_increment=0.001,
            page_increment=0.01,
        )
        w_y_offset = Gtk.Scrollbar(
            orientation=Gtk.Orientation.VERTICAL, adjustment=adj_y_offset
        )
        config.bind_property(
            "y_offset", adj_y_offset, "value", GObject.BindingFlags.DEFAULT
        )
        preview_grid.attach(w_y_offset, 0, 1, 1, 1)  # Column 0, Row 1
        w_y_offset.set_hexpand(False)
        w_y_offset.set_vexpand(True)

        # Preview Area (Center)
        preview_area = Gtk.DrawingArea()
        preview_area.set_size_request(600, 400)
        preview_frame = Gtk.Frame()
        preview_frame.set_shadow_type(Gtk.ShadowType.IN)  # Add a 1px border
        preview_frame.add(preview_area)
        preview_grid.attach(preview_frame, 1, 1, 1, 1)  # Column 1, Row 1
        preview_frame.set_hexpand(True)
        preview_frame.set_vexpand(True)

        # X-Scale Scrollbar (Bottom, Horizontal)
        adj_x_scale = Gtk.Adjustment(
            value=config.get_property("x_scale"),
            lower=1.0,
            upper=2.0,
            step_increment=0.01,
            page_increment=0.1,
        )
        w_x_scale = Gtk.Scrollbar(
            orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj_x_scale
        )
        config.bind_property(
            "x_scale", adj_x_scale, "value", GObject.BindingFlags.DEFAULT
        )
        preview_grid.attach(w_x_scale, 1, 2, 1, 1)  # Column 1, Row 2
        w_x_scale.set_hexpand(True)
        w_x_scale.set_vexpand(False)

        # Y-Scale Scrollbar (Right, Vertical)
        adj_y_scale = Gtk.Adjustment(
            value=config.get_property("y_scale"),
            lower=1.0,
            upper=2.0,
            step_increment=0.01,
            page_increment=0.1,
        )
        w_y_scale = Gtk.Scrollbar(
            orientation=Gtk.Orientation.VERTICAL, adjustment=adj_y_scale
        )
        config.bind_property(
            "y_scale", adj_y_scale, "value", GObject.BindingFlags.DEFAULT
        )
        preview_grid.attach(w_y_scale, 2, 1, 1, 1)  # Column 2, Row 1
        w_y_scale.set_hexpand(False)
        w_y_scale.set_vexpand(True)

        # Lock Checkbox (Bottom Right)
        # Using GimpUi.ChainButton as requested by the user
        w_lock_scale = GimpUi.ChainButton.new(GimpUi.ChainPosition.BOTTOM)
        w_lock_scale.set_active(True)  # Default to locked
        config.bind_property(
            "lock_scale", w_lock_scale, "active", GObject.BindingFlags.DEFAULT
        )
        preview_grid.attach(w_lock_scale, 2, 2, 1, 1)  # Column 2, Row 2
        w_lock_scale.set_hexpand(False)
        w_lock_scale.set_vexpand(False)

        # Bottom Info Label
        bottom_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bottom_hbox.set_border_width(6)
        main_vbox.pack_start(bottom_hbox, False, True, 0)
        info_label = Gtk.Label(label="DPI: ... Crop: ...")
        bottom_hbox.pack_start(info_label, True, True, 0)  # Changed expand to True

        # --- Draw Handler ---
        def draw_preview(widget, cr):
            alloc = widget.get_allocation()
            preview_width = alloc.width
            preview_height = alloc.height

            cr.set_source_rgb(0.2, 0.2, 0.2)
            cr.rectangle(0, 0, preview_width, preview_height)
            cr.fill()

            # 1. Get a high-resolution base thumbnail (max 1024px)
            base_thumbnail = _get_base_thumbnail(image)

            if base_thumbnail:
                # 2. Get slider values
                x_scale = adj_x_scale.get_value()
                y_scale = adj_y_scale.get_value()
                x_offset = adj_x_offset.get_value()
                y_offset = adj_y_offset.get_value()

                # 3. Apply zoom and pan to create the final high-res view
                final_thumbnail = _get_zoomed_view(
                    base_thumbnail, x_scale, y_scale, x_offset, y_offset
                )

                # 4. Scale the final view down to fit the actual drawing area
                thumb_w = final_thumbnail.get_width()
                thumb_h = final_thumbnail.get_height()
                thumb_aspect = thumb_w / thumb_h
                preview_aspect = preview_width / preview_height

                if thumb_aspect > preview_aspect:
                    display_w = preview_width
                    display_h = int(display_w / thumb_aspect)
                else:
                    display_h = preview_height
                    display_w = int(display_h * thumb_aspect)

                # Ensure dimensions are at least 1
                display_w = max(1, display_w)
                display_h = max(1, display_h)

                display_thumbnail = final_thumbnail.scale_simple(
                    display_w, display_h, GdkPixbuf.InterpType.BILINEAR
                )

                # 5. Draw the display-sized thumbnail, centered
                if display_thumbnail:
                    dest_x = (preview_width - display_w) // 2
                    dest_y = (preview_height - display_h) // 2
                    Gdk.cairo_set_source_pixbuf(cr, display_thumbnail, dest_x, dest_y)
                    cr.paint()

                    # 6. Draw overlays based on the display-sized thumbnail
                    _draw_overlays(cr, config, display_w, display_h, dest_x, dest_y)

            elif image is not None:
                # Fallback if thumbnail creation fails
                cr.set_source_rgb(0.5, 0.5, 0.5)
                cr.move_to(0, 0)
                cr.line_to(preview_width, preview_height)
                cr.move_to(preview_width, 0)
                cr.line_to(0, preview_height)
                cr.stroke()

            return False

        preview_area.connect("draw", draw_preview)

        # Show all widgets
        dialog.get_content_area().show_all()

        # --- Logic and Signals ---
        def update_calculations(*args):
            if image is None:
                info_label.set_text("No image open")
                return

            img_width_px = image.get_width()
            img_height_px = image.get_height()

            target_width = config.get_property("width")
            target_height = config.get_property("height")
            unit = config.get_property("unit")

            if target_width == 0 or target_height == 0:
                info_label.set_text("Width or Height cannot be zero.")
                return

            # Use Gimp.Unit.get_factor to get conversion to inches (the base unit)
            conversion_factor = Gimp.Unit.get_factor(unit)
            if conversion_factor == 0:
                info_label.set_text(
                    f"Cannot get conversion factor for '{Gimp.Unit.get_symbol(unit)}'."
                )
                return

            target_width_in = target_width / conversion_factor
            target_height_in = target_height / conversion_factor

            if target_width_in == 0 or target_height_in == 0:
                info_label.set_text("Dimension in inches is zero.")
                return

            dpi_x = img_width_px / target_width_in
            dpi_y = img_height_px / target_height_in

            x_offset = adj_x_offset.get_value()
            y_offset = adj_y_offset.get_value()
            x_scale = adj_x_scale.get_value()
            y_scale = adj_y_scale.get_value()

            info_label.set_text(
                f"X-DPI: {dpi_x:.0f}, Y-DPI: {dpi_y:.0f} | Target: {target_width_in:.2f}x{target_height_in:.2f}in | Scale: {x_scale:.2f}, {y_scale:.2f} | Offset: {x_offset:.2f}, {y_offset:.2f}"
            )

            # Trigger a redraw of the preview area
            preview_area.queue_draw()

        # Connect signals
        for prop in [
            "width",
            "height",
            "unit",
            # x_scale and y_scale are connected directly below
        ]:
            config.connect(f"notify::{prop}", update_calculations)

        # Logic for linking scale sliders
        def on_scale_changed(adjustment, other_adjustment, lock_button):
            if lock_button.get_active():
                new_value = adjustment.get_value()

                prop_name_to_update = None
                if other_adjustment == adj_x_scale:
                    prop_name_to_update = "x_scale"
                elif other_adjustment == adj_y_scale:
                    prop_name_to_update = "y_scale"

                if (
                    prop_name_to_update
                    and config.get_property(prop_name_to_update) != new_value
                ):
                    config.set_property(prop_name_to_update, new_value)

        # Connect scale adjustments to the linking logic
        adj_x_scale.connect(
            "value-changed", on_scale_changed, adj_y_scale, w_lock_scale
        )
        adj_y_scale.connect(
            "value-changed", on_scale_changed, adj_x_scale, w_lock_scale
        )

        # Explicitly connect the offset sliders' adjustments to the update function.
        # The Gimp.Config "notify" signal seems unreliable for standard Gtk widgets.
        adj_x_offset.connect("value-changed", update_calculations)
        adj_y_offset.connect("value-changed", update_calculations)

        # Explicitly connect the scale sliders' adjustments to the update function
        # to ensure immediate redraws.
        adj_x_scale.connect("value-changed", update_calculations)
        adj_y_scale.connect("value-changed", update_calculations)

        if not dialog.run():
            dialog.destroy()
            return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, None)
        else:
            dialog.destroy()

    # Get properties after the dialog has run or from the passed-in config
    width = config.get_property("width")
    height = config.get_property("height")
    unit = config.get_property("unit")

    # For now, just print the values.
    print(
        f"Dialog values: Width: {width}, Height: {height}, Unit: {Gimp.Unit.get_symbol(unit)}"
    )
    print(f"Preparing to crop to {width}x{height}")

    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, None)


class PerfectFitPrint(Gimp.PlugIn):
    def do_set_i18n(self, name):
        return False

    def do_query_procedures(self):
        return [plug_in_proc]

    def do_create_procedure(self, name):
        procedure = None

        if name == plug_in_proc:
            procedure = Gimp.ImageProcedure.new(
                self, name, Gimp.PDBProcType.PLUGIN, perfectfit_print_run, None
            )
            procedure.set_sensitivity_mask(
                Gimp.ProcedureSensitivityMask.DRAWABLE
                | Gimp.ProcedureSensitivityMask.NO_DRAWABLES
            )
            procedure.set_menu_label("PerfectFit Print_...")
            procedure.set_attribution("Dustin Hollon", "Dustin Hollon", "2025")
            procedure.add_menu_path("<Image>/File")
            procedure.set_documentation(
                "Precisely size images for printing.",
                "A GIMP 3-compatible plugin to prepare images for print at exact width and height.",
                None,
            )

            procedure.add_double_argument(
                "width",
                "Width",
                "Target width for the print",
                0.0,
                200.0,
                10.0,
                GObject.ParamFlags.READWRITE,
            )
            procedure.add_double_argument(
                "height",
                "Height",
                "Target height for the print",
                0.0,
                200.0,
                8.0,
                GObject.ParamFlags.READWRITE,
            )

            procedure.add_unit_argument(
                "unit",
                "Unit",
                "Units for width and height",
                False,
                False,
                Gimp.Unit.inch(),
                GObject.ParamFlags.READWRITE,
            )

            procedure.add_double_argument(
                "x_offset",
                "X-Offset",
                "X-Offset",
                -0.5,
                0.5,
                0.0,
                GObject.ParamFlags.READWRITE,
            )
            procedure.add_double_argument(
                "y_offset",
                "Y-Offset",
                "Y-Offset",
                -0.5,
                0.5,
                0.0,
                GObject.ParamFlags.READWRITE,
            )
            procedure.add_double_argument(
                "x_scale",
                "X-Scale",
                "X-Scale",
                1.0,
                2.0,
                1.0,
                GObject.ParamFlags.READWRITE,
            )
            procedure.add_double_argument(
                "y_scale",
                "Y-Scale",
                "Y-Scale",
                1.0,
                2.0,
                1.0,
                GObject.ParamFlags.READWRITE,
            )
            procedure.add_boolean_argument(
                "lock_scale",
                "Lock Scale",
                "Lock X and Y scale",
                True,
                GObject.ParamFlags.READWRITE,
            )

        return procedure


Gimp.main(PerfectFitPrint.__gtype__, sys.argv)
