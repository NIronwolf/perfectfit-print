#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gi

gi.require_version("Gimp", "3.0")
from gi.repository import Gimp

gi.require_version("GimpUi", "3.0")
from gi.repository import GimpUi
from gi.repository import GObject, GLib, Gtk, Gdk, GdkPixbuf, Gio

import sys

plug_in_proc = "plug-in-perfectfit-print"
plug_in_binary = "perfectfit-print"


# ---------- Thumbnail / preview helpers ----------

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


def _get_zoomed_view(base_thumbnail, x_scale, y_scale):
    """
    Takes a base thumbnail and scale values, and returns a zoomed pixbuf.

    Returns a larger pixbuf containing the entire zoomed image.
    Panning/offset will be handled during display.
    """
    if not base_thumbnail:
        return base_thumbnail

    thumb_w = base_thumbnail.get_width()
    thumb_h = base_thumbnail.get_height()

    # Calculate zoomed dimensions
    zoomed_w = int(thumb_w * x_scale)
    zoomed_h = int(thumb_h * y_scale)

    zoomed_pixbuf = GdkPixbuf.Pixbuf.new(
        GdkPixbuf.Colorspace.RGB,
        base_thumbnail.get_has_alpha(),
        base_thumbnail.get_bits_per_sample(),
        zoomed_w,
        zoomed_h,
    )
    if base_thumbnail.get_has_alpha():
        zoomed_pixbuf.fill(0x00000000)

    # Just zoom, no panning
    base_thumbnail.scale(
        dest=zoomed_pixbuf,
        dest_x=0,
        dest_y=0,
        dest_width=zoomed_w,
        dest_height=zoomed_h,
        offset_x=0,
        offset_y=0,
        scale_x=x_scale,
        scale_y=y_scale,
        interp_type=GdkPixbuf.InterpType.BILINEAR,
    )

    return zoomed_pixbuf


def _draw_overlays(
    cr, thumb_w, thumb_h, dest_x, dest_y, x_offset, y_offset, crop_w, crop_h
):
    """
    Draws the dimming overlay and the dashed crop rectangle.

    Args:
        cr: Cairo context
        thumb_w, thumb_h: Displayed thumbnail dimensions
        dest_x, dest_y: Position where thumbnail is drawn
        x_offset, y_offset: Crop position offsets
        crop_w, crop_h: Crop rectangle dimensions
    """

    x_slop = thumb_w - crop_w
    y_slop = thumb_h - crop_h

    # Calculate crop position and clamp to ensure it stays on-screen
    crop_x = max(0, (dest_x + (x_offset + 0.5) * x_slop))
    crop_y = max(0, (dest_y + (y_offset + 0.5) * y_slop))

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


# ---------- Export math helpers ----------

def _compute_target_inches(config):
    """
    Return (width_in, height_in) from width/height/unit in config.
    Returns (None, None) if invalid.
    """
    target_width = config.get_property("width")
    target_height = config.get_property("height")
    unit = config.get_property("unit")

    if target_width <= 0 or target_height <= 0:
        return None, None

    factor = Gimp.Unit.get_factor(unit)
    if factor == 0:
        return None, None

    target_width_in = target_width / factor
    target_height_in = target_height / factor

    if target_width_in <= 0 or target_height_in <= 0:
        return None, None

    return target_width_in, target_height_in


def _compute_crop_and_dpi(new_image, config):
    """
    Compute:
      - dpi_x, dpi_y
      - crop origin (offx, offy) in image pixels
      - crop size (width, height) in image pixels
    based on the same conceptual model as the preview.
    """
    img_width_px = new_image.get_width()
    img_height_px = new_image.get_height()

    target_width_in, target_height_in = _compute_target_inches(config)
    if not target_width_in or not target_height_in:
        return None

    x_scale = config.get_property("x_scale")
    y_scale = config.get_property("y_scale")
    x_offset = config.get_property("x_offset")
    y_offset = config.get_property("y_offset")

    # Conceptual "selection" size in the original image
    full_sel_w = img_width_px / x_scale
    full_sel_h = img_height_px / y_scale

    selection_aspect = full_sel_w / full_sel_h
    target_aspect = target_width_in / target_height_in

    if target_aspect > selection_aspect:
        cropped_sel_w = full_sel_w
        cropped_sel_h = cropped_sel_w / target_aspect
    else:
        cropped_sel_h = full_sel_h
        cropped_sel_w = cropped_sel_h * target_aspect

    # DPI from cropped selection vs target inches
    dpi_x = cropped_sel_w / target_width_in
    dpi_y = cropped_sel_h / target_height_in

    # Center the conceptual selection in the image
    sel_origin_x = (img_width_px - full_sel_w) / 2.0
    sel_origin_y = (img_height_px - full_sel_h) / 2.0

    # Offset inside that selection, x_offset/y_offset in [-0.5, 0.5]
    crop_offset_x = (full_sel_w - cropped_sel_w) * (x_offset + 0.5)
    crop_offset_y = (full_sel_h - cropped_sel_h) * (y_offset + 0.5)

    crop_origin_x = sel_origin_x + crop_offset_x
    crop_origin_y = sel_origin_y + crop_offset_y

    return {
        "dpi_x": dpi_x,
        "dpi_y": dpi_y,
        "offx": int(round(crop_origin_x)),
        "offy": int(round(crop_origin_y)),
        "width": int(round(cropped_sel_w)),
        "height": int(round(cropped_sel_h)),
        "target_width_in": target_width_in,
        "target_height_in": target_height_in,
    }

# ---------- Main procedure ----------

def perfectfit_print_run(procedure, run_mode, image, drawables, config, data):
    if run_mode != Gimp.RunMode.INTERACTIVE:
        # NON-INTERACTIVE mode (for scripting) – minimal
        width = config.get_property("width")
        height = config.get_property("height")
        unit = config.get_property("unit")
        print(f"Non-interactive run: {width}x{height} {Gimp.Unit.get_symbol(unit)}")
        return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, None)

    # ---------- INTERACTIVE MODE UI ----------

    GimpUi.init(plug_in_binary)
    dialog = GimpUi.ProcedureDialog.new(procedure, config, "PerfectFit Print")

    main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    dialog.get_content_area().add(main_vbox)

    # ===== Top settings grid =====
    settings_grid = Gtk.Grid()
    settings_grid.set_column_spacing(12)
    settings_grid.set_row_spacing(6)
    settings_grid.set_border_width(6)
    main_vbox.pack_start(settings_grid, False, False, 0)

    # Width
    label = Gtk.Label.new_with_mnemonic("_Width:")
    settings_grid.attach(label, 0, 0, 1, 1)
    w_width = GimpUi.prop_spin_button_new(config, "width", 0.05, 1.0, 2)
    label.set_mnemonic_widget(w_width)
    settings_grid.attach(w_width, 1, 0, 1, 1)

    # Height
    label = Gtk.Label.new_with_mnemonic("_Height:")
    settings_grid.attach(label, 2, 0, 1, 1)
    w_height = GimpUi.prop_spin_button_new(config, "height", 0.05, 1.0, 2)
    label.set_mnemonic_widget(w_height)
    settings_grid.attach(w_height, 3, 0, 1, 1)

    # Unit
    label = Gtk.Label.new_with_mnemonic("_Unit:")
    settings_grid.attach(label, 4, 0, 1, 1)
    w_unit = GimpUi.prop_unit_combo_box_new(config, "unit")
    label.set_mnemonic_widget(w_unit)
    settings_grid.attach(w_unit, 5, 0, 1, 1)

    # File Format
    label = Gtk.Label.new_with_mnemonic("F_ormat:")
    settings_grid.attach(label, 6, 0, 1, 1)
    format_store = Gtk.ListStore(str)
    for item in ["PNG", "JPEG", "TIFF"]:
        format_store.append([item])
    w_format = GimpUi.prop_string_combo_box_new(
        config,
        "file_format",
        format_store,
        0,
        0,
    )
    label.set_mnemonic_widget(w_format)
    settings_grid.attach(w_format, 7, 0, 1, 1)

    # ===== Middle preview + scrollbars =====
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
        "x_offset",
        adj_x_offset,
        "value",
        GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
    )
    preview_grid.attach(w_x_offset, 1, 0, 1, 1)
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
        "y_offset",
        adj_y_offset,
        "value",
        GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
    )
    preview_grid.attach(w_y_offset, 0, 1, 1, 1)
    w_y_offset.set_hexpand(False)
    w_y_offset.set_vexpand(True)

    # Preview Area (Center)
    preview_area = Gtk.DrawingArea()
    preview_area.set_size_request(600, 400)
    preview_frame = Gtk.Frame()
    preview_frame.set_shadow_type(Gtk.ShadowType.IN)
    preview_frame.add(preview_area)
    preview_grid.attach(preview_frame, 1, 1, 1, 1)
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
        "x_scale",
        adj_x_scale,
        "value",
        GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
    )
    preview_grid.attach(w_x_scale, 1, 2, 1, 1)
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
        "y_scale",
        adj_y_scale,
        "value",
        GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
    )
    preview_grid.attach(w_y_scale, 2, 1, 1, 1)
    w_y_scale.set_hexpand(False)
    w_y_scale.set_vexpand(True)

    # Lock Checkbox (Bottom Right)
    w_lock_scale = GimpUi.ChainButton.new(GimpUi.ChainPosition.BOTTOM)
    w_lock_scale.set_active(True)  # Default to locked
    config.bind_property(
        "lock_scale",
        w_lock_scale,
        "active",
        GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
    )
    preview_grid.attach(w_lock_scale, 2, 2, 1, 1)
    w_lock_scale.set_hexpand(False)
    w_lock_scale.set_vexpand(False)

    # Bottom Info Label
    bottom_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    bottom_hbox.set_border_width(6)
    main_vbox.pack_start(bottom_hbox, False, True, 0)
    info_label = Gtk.Label(label="DPI: ... Crop: ...")
    bottom_hbox.pack_start(info_label, True, True, 0)

    # ---------- Draw handler for the preview ----------

    def draw_preview(widget, cr):
        alloc = widget.get_allocation()
        preview_width = alloc.width
        preview_height = alloc.height

        cr.set_source_rgb(0.2, 0.2, 0.2)
        cr.rectangle(0, 0, preview_width, preview_height)
        cr.fill()

        base_thumbnail = _get_base_thumbnail(image)
        if not base_thumbnail:
            if image is not None:
                cr.set_source_rgb(0.5, 0.5, 0.5)
                cr.move_to(0, 0)
                cr.line_to(preview_width, preview_height)
                cr.move_to(preview_width, 0)
                cr.line_to(0, preview_height)
                cr.stroke()
            return False

        x_scale = adj_x_scale.get_value()
        y_scale = adj_y_scale.get_value()
        x_offset = adj_x_offset.get_value()
        y_offset = adj_y_offset.get_value()

        zoomed_thumbnail = _get_zoomed_view(base_thumbnail, x_scale, y_scale)

        base_w = base_thumbnail.get_width()
        base_h = base_thumbnail.get_height()
        thumb_w = zoomed_thumbnail.get_width()
        thumb_h = zoomed_thumbnail.get_height()

        target_w_prop = config.get_property("width")
        target_h_prop = config.get_property("height")

        base_aspect = base_w / base_h
        target_aspect = (
            target_w_prop / target_h_prop if target_h_prop > 0 else base_aspect
        )

        # Determine crop rectangle size in the BASE thumbnail
        if target_aspect > base_aspect:
            crop_w_in_base = base_w
            crop_h_in_base = crop_w_in_base / target_aspect
        else:
            crop_h_in_base = base_h
            crop_w_in_base = crop_h_in_base * target_aspect

        # Scale the crop rectangle to fill the preview
        scale_for_crop_w = preview_width / crop_w_in_base
        scale_for_crop_h = preview_height / crop_h_in_base
        display_scale = min(scale_for_crop_w, scale_for_crop_h)

        # Size for zoomed thumbnail in the preview
        display_w = max(1, int(thumb_w * display_scale))
        display_h = max(1, int(thumb_h * display_scale))

        display_thumbnail = zoomed_thumbnail.scale_simple(
            display_w, display_h, GdkPixbuf.InterpType.BILINEAR
        )

        if display_thumbnail:
            # Crop dimensions in display space
            crop_w_display = int(crop_w_in_base * display_scale)
            crop_h_display = int(crop_h_in_base * display_scale)

            # Available space around crop in the displayed image
            x_slop_display = display_w - crop_w_display
            y_slop_display = display_h - crop_h_display

            # Available space in preview around the crop
            x_preview_space = preview_width - crop_w_display
            y_preview_space = preview_height - crop_h_display

            # How much to shift image so crop travels correctly with offset
            x_shift_needed = x_slop_display - x_preview_space
            y_shift_needed = y_slop_display - y_preview_space

            # Center image in preview
            dest_x = (preview_width - display_w) // 2
            dest_y = (preview_height - display_h) // 2

            # Apply offset-based shift
            dest_x -= int(x_offset * x_shift_needed)
            dest_y -= int(y_offset * y_shift_needed)

            Gdk.cairo_set_source_pixbuf(cr, display_thumbnail, dest_x, dest_y)
            cr.paint()

            # Draw overlays
            _draw_overlays(
                cr,
                display_w,
                display_h,
                dest_x,
                dest_y,
                x_offset,
                y_offset,
                crop_w_display,
                crop_h_display,
            )

        return False

    preview_area.connect("draw", draw_preview)

    # Show all widgets
    dialog.get_content_area().show_all()

    # ---------- Logic and signals ----------

    def update_calculations(*args):
        if image is None:
            info_label.set_text("No image open")
            return

        img_width_px = image.get_width()
        img_height_px = image.get_height()

        target_width = config.get_property("width")
        target_height = config.get_property("height")
        unit = config.get_property("unit")

        if target_width <= 0 or target_height <= 0:
            info_label.set_text("Width and Height must be positive.")
            return

        factor = Gimp.Unit.get_factor(unit)
        if factor == 0:
            info_label.set_text(
                f"Cannot get conversion factor for '{Gimp.Unit.get_symbol(unit)}'."
            )
            return

        target_width_in = target_width / factor
        target_height_in = target_height / factor

        if target_width_in <= 0 or target_height_in <= 0:
            info_label.set_text("Dimensions in inches must be positive.")
            return

        x_scale = adj_x_scale.get_value()
        y_scale = adj_y_scale.get_value()
        x_offset = adj_x_offset.get_value()
        y_offset = adj_y_offset.get_value()

        full_sel_w = img_width_px / x_scale
        full_sel_h = img_height_px / y_scale

        selection_aspect = full_sel_w / full_sel_h
        target_aspect = target_width_in / target_height_in

        if target_aspect > selection_aspect:
            cropped_sel_w = full_sel_w
            cropped_sel_h = cropped_sel_w / target_aspect
        else:
            cropped_sel_h = full_sel_h
            cropped_sel_w = cropped_sel_h * target_aspect

        dpi_x = cropped_sel_w / target_width_in
        dpi_y = cropped_sel_h / target_height_in

        info_label.set_text(
            f"X-DPI: {dpi_x:.0f}, Y-DPI: {dpi_y:.0f} | "
            f"Target: {target_width_in:.2f}x{target_height_in:.2f}in | "
            f"Scale: {x_scale:.2f}, {y_scale:.2f} | "
            f"Offset: {x_offset:.2f}, {y_offset:.2f}"
        )

        preview_area.queue_draw()

    # React when config properties themselves change
    for prop in ["width", "height", "unit"]:
        config.connect(f"notify::{prop}", update_calculations)

    # Linking scale sliders when lock is active
    def on_scale_changed(adjustment, other_adjustment, lock_button):
        if not lock_button.get_active():
            return

        new_value = adjustment.get_value()

        if other_adjustment == adj_x_scale:
            prop_name = "x_scale"
        elif other_adjustment == adj_y_scale:
            prop_name = "y_scale"
        else:
            return

        if config.get_property(prop_name) != new_value:
            config.set_property(prop_name, new_value)

    adj_x_scale.connect("value-changed", on_scale_changed, adj_y_scale, w_lock_scale)
    adj_y_scale.connect("value-changed", on_scale_changed, adj_x_scale, w_lock_scale)

    # Redraw preview whenever sliders move
    adj_x_offset.connect("value-changed", update_calculations)
    adj_y_offset.connect("value-changed", update_calculations)
    adj_x_scale.connect("value-changed", update_calculations)
    adj_y_scale.connect("value-changed", update_calculations)

    # Initial state
    update_calculations()

    dialog_result = dialog.run()
    if not dialog_result:
        dialog.destroy()
        return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, None)

    # ---------- EXPORT (INTERACTIVE) ----------

    try:
        file_format = config.get_property("file_format")
        extension = file_format.lower()

        # File chooser dialog
        file_chooser = Gtk.FileChooserDialog(
            title=f"Export as {file_format}",
            parent=dialog,
            action=Gtk.FileChooserAction.SAVE,
        )
        file_chooser.add_buttons(
            "_Cancel", Gtk.ResponseType.CANCEL, "_Export", Gtk.ResponseType.OK
        )
        file_chooser.set_do_overwrite_confirmation(True)

        # Suggest a filename
        suggested_name = "untitled"
        image_file = image.get_file()
        if image_file:
            path = image_file.get_path()
            if path:
                base_name = path.split("\\")[-1].split("/")[-1]
                suggested_name = base_name.rsplit(".", 1)[0]
        file_chooser.set_current_name(f"{suggested_name}-perfectfit.{extension}")

        file_response = file_chooser.run()
        save_path = None
        if file_response == Gtk.ResponseType.OK:
            save_path = file_chooser.get_filename()

        file_chooser.destroy()
        dialog.destroy()

        if not save_path:
            return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, None)

        # --- Start non-destructive export ---

        # 1. Duplicate the image
        new_image = image.duplicate()
        if not new_image:
            Gimp.message("Failed to duplicate image.")
            return procedure.new_return_values(
                Gimp.PDBStatusType.EXECUTION_ERROR, None
            )

        # 2. Compute crop + DPI from config
        export = _compute_crop_and_dpi(new_image, config)
        if not export:
            Gimp.message("Invalid target size or unit.")
            return procedure.new_return_values(
                Gimp.PDBStatusType.EXECUTION_ERROR, None
            )

        dpi_x = export["dpi_x"]
        dpi_y = export["dpi_y"]
        crop_origin_x_px = export["offx"]
        crop_origin_y_px = export["offy"]
        crop_width_px = export["width"]
        crop_height_px = export["height"]

        # Set the resolution
        new_image.set_resolution(dpi_x, dpi_y)

        # 3. Crop the image
        proc = Gimp.get_pdb().lookup_procedure("gimp-image-crop")
        crop_config = proc.create_config()
        crop_config.set_property("image", new_image)
        crop_config.set_property("new-width", crop_width_px)
        crop_config.set_property("new-height", crop_height_px)
        crop_config.set_property("offx", crop_origin_x_px)
        crop_config.set_property("offy", crop_origin_y_px)
        proc.run(crop_config)

        # 4. Save the file
        gfile = Gio.file_new_for_path(save_path)

        proc = Gimp.get_pdb().lookup_procedure("gimp-file-save")
        save_config = proc.create_config()
        save_config.set_property("run-mode", Gimp.RunMode.NONINTERACTIVE)
        save_config.set_property("image", new_image)
        save_config.set_property("file", gfile)

        # Let GIMP create default export options (None is allowed)
        save_config.set_property("options", None)
        proc.run(save_config)

        # 5. Clean up the duplicated image
        proc = Gimp.get_pdb().lookup_procedure("gimp-image-delete")
        delete_config = proc.create_config()
        delete_config.set_property("image", new_image)
        proc.run(delete_config)

        Gimp.message("Done!")

    except Exception as e:
        import traceback

        Gimp.message(
            "An error occurred during export:\n\n"
            f"{e}\n\n{traceback.format_exc()}"
        )
        if "new_image" in locals() and new_image and new_image.is_valid():
            proc = Gimp.get_pdb().lookup_procedure("gimp-image-delete")
            delete_config = proc.create_config()
            delete_config.set_property("image", new_image)
            proc.run(delete_config)

        if "dialog" in locals() and dialog:
            dialog.destroy()

        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, None)

    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, None)


# ---------- Plug-in class ----------

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

            procedure.add_string_argument(
                "file_format",
                "File Format",
                "Export file format (PNG, JPEG, TIFF)",
                "PNG",
                GObject.ParamFlags.READWRITE,
            )

        return procedure


Gimp.main(PerfectFitPrint.__gtype__, sys.argv)