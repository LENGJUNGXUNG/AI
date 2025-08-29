import os
import io
import re
import traceback
import hashlib
from flask import Flask, request, jsonify, send_file
from werkzeug.utils import secure_filename
from flask_cors import CORS
import fitz  # PyMuPDF
from PIL import Image as PILImage
from reportlab.lib.pagesizes import letter
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Image, PageBreak, Table, TableStyle, KeepTogether, Spacer
)
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
import camelot


# --- GLOBAL SETTINGS ---
# Set this to True to force all tables to be rendered as images. This
# guarantees that diagrams or images inside a table are captured correctly.
# If False, the program will try to extract the table as text and only rasterize
# if an embedded image is detected or the text extraction is poor.
FORCE_RASTERIZE_ALL_TABLES = True

# --- FLASK APP SETUP ---
app = Flask(__name__)
CORS(app)
app.config['UPLOAD_FOLDER'] = 'extract_file'


# --- IMAGE + CAPTION + DESCRIPTION EXTRACTION ---
def extract_images_with_captions_and_descriptions(pdf_path):
    """
    Extract images and their complete descriptions from the PDF.
    This function finds image blocks and looks for nearby text that
    matches common caption patterns (e.g., "Figure 1", "Fig. 2.3", "Diagram 4").
    It then creates a composite rasterized image of the figure plus its
    caption and description to preserve the original layout.

    Returns a list of dictionaries, where each dict represents a figure entry.
    """
    document = fitz.open(pdf_path)
    temp_image_dir = "temp_extracted_images"
    if not os.path.exists(temp_image_dir):
        os.makedirs(temp_image_dir)

    image_entries = []
    seen_hashes = set()

    for page_num in range(len(document)):
        page = document.load_page(page_num)
        images = page.get_images(full=True)
        text_blocks = sorted(page.get_text("blocks"), key=lambda b: (b[1], b[0]))
        
        # Regex to find common image/figure captions
        cap_patterns = [
            r".*?(figure|fig\.?|table|diagram)\b",
            r"^(figure|fig\.?|table|diagram)\s*\d+",
            r".*?(fig\.?\s*\d+)",
            r".*?(figure\s*\d+)",
        ]
        cap_re = re.compile("|".join(cap_patterns), re.IGNORECASE)

        for i, img_data in enumerate(images):
            xref = img_data[0]
            base_image = document.extract_image(xref)
            image_bytes = base_image["image"]
            image_ext = base_image["ext"]

            # Use a hash to avoid processing the same image twice (common in PDFs)
            h = hashlib.md5(image_bytes).hexdigest()
            if h in seen_hashes:
                continue
            seen_hashes.add(h)

            # Get the image's bounding box on the page
            rect = page.get_image_bbox(img_data)
            if not rect:
                continue

            # Find the nearest caption block
            best_idx = None
            best_distance = float('inf')
            
            # Search for a caption below the image first, then above
            for idx, block in enumerate(text_blocks):
                bx, by, bw, bh, bt, *_ = block
                t = bt.strip()
                if not t or not cap_re.search(t):
                    continue
                
                # Check for proximity to the image's bounding box
                # Below
                if 0 <= by - rect.y1 <= 150 and abs(bx - rect.x0) < rect.width:
                    d = by - rect.y1
                    if d < best_distance:
                        best_distance = d
                        best_idx = idx
                # Above
                elif 0 <= rect.y0 - (by + bh) <= 100 and abs(bx - rect.x0) < rect.width:
                    d = rect.y0 - (by + bh)
                    if d < best_distance:
                        best_distance = d
                        best_idx = idx

            caption_text = None
            description_text = None
            composite_path = None
            
            if best_idx is not None:
                # Merge the caption block and any subsequent description blocks
                caption_text, description_text, caption_rect, desc_rect = _merge_text_blocks(
                    text_blocks, best_idx, cap_re
                )

                # Create a composite clip that includes the image, caption, and description
                clip = fitz.Rect(rect)
                if caption_rect:
                    clip |= caption_rect
                if desc_rect:
                    clip |= desc_rect
                
                # Add a small padding to the clip for clean borders
                pad = 4
                clip = fitz.Rect(
                    max(0, clip.x0 - pad),
                    max(0, clip.y0 - pad),
                    clip.x1 + pad,
                    clip.y1 + pad
                )

                try:
                    # Rasterize the composite clip into a single image
                    pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(2, 2))
                    composite_path = os.path.join(
                        temp_image_dir, f"figure_p{page_num+1}_{i+1}_composite.png"
                    )
                    pix.save(composite_path)
                    print(f"📸 Created composite figure image for page {page_num+1}.")
                except Exception as e_img:
                    print(f"⚠️ Could not rasterize composite figure on page {page_num+1}: {e_img}")

            # Save the original image as a fallback
            image_path = os.path.join(temp_image_dir, f"page_{page_num+1}_{i+1}.{image_ext}")
            try:
                img = PILImage.open(io.BytesIO(image_bytes))
                if img.mode == "CMYK":
                    img = img.convert("RGB")
                img.save(image_path)
            except Exception as e:
                print(f"❌ Could not save image from page {page_num+1}: {e}")
                continue

            image_entries.append({
                'type': 'image',
                'page': page_num + 1,
                'path': image_path,
                'composite_path': composite_path,
                'bbox': rect,
                'caption': caption_text,
                'description': description_text,
                'hash': h,
            })

    document.close()
    return image_entries


# --- TABLE EXTRACTION WITH DESCRIPTIONS ---
def extract_tables_with_captions_and_descriptions(pdf_path):
    """
    Extracts tables using Camelot and attempts to find their captions and descriptions.
    The function is designed to handle tables with embedded diagrams by rasterizing them.

    Returns a list of dictionaries, where each dict represents a table entry.
    """
    tables_with_captions = []
    temp_image_dir = "temp_extracted_images"
    if not os.path.exists(temp_image_dir):
        os.makedirs(temp_image_dir)
        
    try:
        # Use Camelot's lattice mode first, which is better for structured tables
        tables = camelot.read_pdf(pdf_path, pages="all", flavor="lattice")
        document = fitz.open(pdf_path)
        seen_tables = set()

        def good_tables(iterable):
            """Filter for tables that meet a minimum quality standard."""
            for t in iterable:
                data = t.df.values.tolist()
                rows = len(data)
                cols = len(data[0]) if rows else 0
                flat = [str(c).strip() for row in data for c in row]
                non_empty = sum(1 for c in flat if c)
                # Check for min size and content ratio
                if rows < 2 or cols < 2 or (non_empty / max(1, len(flat))) < 0.15:
                    continue
                yield t, data

        extracted_any = False
        for t, data in good_tables(tables):
            # Check for duplicate tables on the same page
            serialized = f"{t.page}|{repr(data)}"
            if serialized in seen_tables:
                continue
            seen_tables.add(serialized)

            page = document.load_page(t.page - 1)
            text_blocks = sorted(page.get_text("blocks"), key=lambda b: (b[1], b[0]))
            
            caption_text = None
            description_text = None
            y_hint = 999999.0
            
            # Regex for table captions
            table_cap_re = re.compile(r".*?(table)\b", re.IGNORECASE)

            # Get the table's bounding box from Camelot
            bbox = getattr(t, 'bbox', None) or getattr(t, '_bbox', None)
            table_rect = None
            if bbox is not None:
                x1, y1, x2, y2 = bbox
                page_h = page.rect.height
                table_rect = fitz.Rect(x1, page_h - y2, x2, page_h - y1)

            # Find the nearest caption block to the table
            best_idx = None
            best_distance = float('inf')
            for idx, block in enumerate(text_blocks):
                bx, by, bw, bh, bt, *_ = block
                ttext = bt.strip()
                if not table_cap_re.search(ttext) or not table_rect:
                    continue
                
                # Check for proximity
                if 0 <= by - table_rect.y1 <= 150 and abs(bx - table_rect.x0) < table_rect.width:
                    d = by - table_rect.y1
                    if d < best_distance:
                        best_distance = d
                        best_idx = idx
                elif 0 <= table_rect.y0 - (by + bh) <= 100 and abs(bx - table_rect.x0) < table_rect.width:
                    d = table_rect.y0 - (by + bh)
                    if d < best_distance:
                        best_distance = d
                        best_idx = idx
            
            if best_idx is not None:
                caption_text, description_text, caption_rect, desc_rect = _merge_text_blocks(
                    text_blocks, best_idx, table_cap_re
                )
                y_hint = text_blocks[best_idx][1]
                print(f"📊 Found caption for table on page {t.page}: '{caption_text}'")
            elif table_rect is not None:
                y_hint = float(table_rect.y0)

            # --- RASTERIZATION LOGIC (CRITICAL FOR YOUR USE CASE) ---
            force_raster = FORCE_RASTERIZE_ALL_TABLES
            clip = None
            
            if table_rect:
                # Check for any images intersecting the table's region
                try:
                    imgs = page.get_images(full=True)
                    for img_data in imgs:
                        irect = page.get_image_bbox(img_data)
                        if irect and irect.intersects(table_rect):
                            force_raster = True
                            print(f"🖼️ Detected an image inside table on page {t.page}, forcing rasterization.")
                            break
                except Exception as _e:
                    pass

                # Build a composite clip of the table, caption, and description
                composite = fitz.Rect(table_rect)
                if 'caption_rect' in locals() and caption_rect:
                    composite |= caption_rect
                if 'desc_rect' in locals() and desc_rect:
                    composite |= desc_rect
                pad = 4
                clip = fitz.Rect(
                    max(0, composite.x0 - pad),
                    max(0, composite.y0 - pad),
                    composite.x1 + pad,
                    composite.y1 + pad
                )
            
            table_entry = {
                'type': 'table',
                'page': t.page,
                'data': data,
                'caption': caption_text,
                'description': description_text,
                'y_hint': y_hint,
            }

            # If rasterization is forced, create the image file
            if force_raster and clip:
                try:
                    pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(2, 2))
                    img_path = os.path.join(
                        temp_image_dir, f"table_p{t.page}_{hash(repr(bbox)) & 0xfffffff}.png"
                    )
                    pix.save(img_path)
                    table_entry['as_image_path'] = img_path
                    table_entry['y_hint'] = float(min(clip.y0, y_hint))
                    print(f"✅ Rasterized table on page {t.page}.")
                except Exception as e_img:
                    print(f"⚠️ Could not rasterize table on page {t.page}: {e_img}")

            tables_with_captions.append(table_entry)
            extracted_any = True
            
        document.close()
        
    except Exception as e:
        print(f"⚠️ Table extraction failed: {e}")
    return tables_with_captions


# --- HELPER FUNCTION FOR TEXT BLOCK MERGING ---
def _merge_text_blocks(text_blocks_sorted, start_idx, cap_re):
    """
    Helper function to merge a starting text block (the caption) with
    any immediately following blocks (the description).
    """
    bx0, by0, bw0, bh0, bt0, *_ = text_blocks_sorted[start_idx]
    caption = bt0.strip()
    last_bottom = by0 + bh0
    min_x = bx0
    max_x = bx0 + bw0
    
    description_parts = []
    desc_min_x = min_x
    desc_max_x = max_x
    desc_top = None
    
    for j in range(start_idx + 1, len(text_blocks_sorted)):
        bx, by, bw, bh, bt, *_ = text_blocks_sorted[j]
        tline = bt.strip()
        if not tline:
            continue
        if cap_re.search(tline):  # Stop if a new caption is found
            break
        # Only merge if the text is vertically close
        if by - last_bottom <= 80:
            description_parts.append(tline)
            last_bottom = by + bh
            desc_top = by if desc_top is None else desc_top
            desc_min_x = min(desc_min_x, bx)
            desc_max_x = max(desc_max_x, bx + bw)
        else:
            break
            
    caption_rect = fitz.Rect(min_x, by0, max_x, by0 + bh0)
    desc_rect = None
    if description_parts and desc_top is not None:
        desc_rect = fitz.Rect(desc_min_x, desc_top, desc_max_x, last_bottom)
    
    description = " ".join(description_parts) if description_parts else None
    return caption, description, caption_rect, desc_rect


# --- PDF BUILD ---
def build_pdf_with_images_and_tables(image_entries, tables, output_filename="images_and_tables.pdf"):
    """
    Build a PDF from the extracted images and tables, maintaining their original order.
    Items are sorted by their original page and vertical position.
    """
    output_dir = os.path.join(os.path.dirname(__file__), "generate-pdf")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    output_path = os.path.join(output_dir, output_filename)
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(buffer, pagesize=letter)
    story = []
    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    
    # Combine and sort all extracted items
    combined = sorted(
        image_entries + tables,
        key=lambda x: (x['page'], x['y_hint'] if 'y_hint' in x else x['bbox'].y0)
    )

    current_page = None
    figure_count = 1
    table_count = 1

    for item in combined:
        if current_page is not None and item['page'] != current_page:
            story.append(PageBreak())
        current_page = item['page']

        if item['type'] == 'image':
            try:
                # Prioritize the composite image if available
                img_source_path = item.get('composite_path') or item['path']
                
                # Use PIL to get dimensions and scale the image
                pil_img = PILImage.open(img_source_path)
                if pil_img.mode != "RGB":
                    pil_img = pil_img.convert("RGB")
                img_width, img_height = pil_img.size
                max_width, max_height = 450, 600
                scale = min(max_width / img_width, max_height / img_height)
                new_width = img_width * scale
                new_height = img_height * scale
                
                img = Image(img_source_path, width=new_width, height=new_height)
                
                # Check if we should add a separate caption/description
                if item.get('composite_path'):
                    # The composite image already includes the text
                    story.append(KeepTogether([img]))
                else:
                    # Manually create the caption and description paragraphs
                    text_parts = []
                    if item.get('caption'):
                        text_parts.append(item['caption'])
                    if item.get('description'):
                        text_parts.append(item['description'])
                    
                    full_text = "\n\n".join(text_parts) if text_parts else f"Figure {figure_count}.0 (Page {item['page']})"
                    story.append(KeepTogether([img, Spacer(1, 6), Paragraph(full_text, normal)]))
                
                figure_count += 1
            except Exception as e:
                print(f"❌ Could not add image {item.get('path')} to PDF: {e}")

        elif item['type'] == 'table':
            try:
                img_path = item.get('as_image_path')
                if img_path and os.path.exists(img_path):
                    # Add the rasterized table image
                    pil_img = PILImage.open(img_path)
                    if pil_img.mode != "RGB":
                        pil_img = pil_img.convert("RGB")
                    img_width, img_height = pil_img.size
                    max_width, max_height = 450, 600
                    scale = min(max_width / img_width, max_height / img_height)
                    img = Image(img_path, width=img_width * scale, height=img_height * scale)
                    story.append(KeepTogether([img]))
                else:
                    # Fallback to a vector table if no image path exists
                    table = Table(item['data'])
                    table.setStyle(TableStyle([
                        ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ]))
                    
                    text_parts = []
                    if item.get('caption'):
                        text_parts.append(item['caption'])
                    if item.get('description'):
                        text_parts.append(item['description'])
                    full_text = "\n\n".join(text_parts) if text_parts else f"Table {table_count}.0 (Page {item['page']})"
                    story.append(KeepTogether([table, Spacer(1, 6), Paragraph(full_text, normal)]))
                
                table_count += 1
            except Exception as e:
                print(f"❌ Could not add table from page {item['page']}: {e}")

    doc.build(story)
    
    buffer.seek(0)
    # Save the file to the local directory as a check
    with open(output_path, "wb") as f:
        f.write(buffer.getbuffer())
    print(f"✅ PDF saved to {output_path}")
    return buffer


# --- ROUTE ---
@app.route('/upload-pdfs', methods=['POST'])
def upload_files():
    """Main route to handle PDF uploads and generate the new PDF."""
    try:
        if 'pdfFile' not in request.files:
            return jsonify({'error': 'No file part in the request'}), 400

        files = request.files.getlist('pdfFile')
        if not files:
            return jsonify({'error': 'No selected files'}), 400

        upload_folder = app.config['UPLOAD_FOLDER']
        if not os.path.exists(upload_folder):
            os.makedirs(upload_folder)

        all_image_entries = []
        all_tables = []

        for file in files:
            filename = secure_filename(file.filename)
            filepath = os.path.join(upload_folder, filename)
            file.save(filepath)

            print(f"Processing {filename}...")
            
            # Extract images and tables
            image_entries = extract_images_with_captions_and_descriptions(filepath)
            all_image_entries.extend(image_entries)
            
            tables = extract_tables_with_captions_and_descriptions(filepath)
            all_tables.extend(tables)

            os.remove(filepath)
            print(f"Cleaned up temporary file {filepath}.")

        if not all_image_entries and not all_tables:
            return jsonify({'error': 'No images or tables found in the uploaded PDF(s).'}), 400

        pdf_buffer = build_pdf_with_images_and_tables(all_image_entries, all_tables, "images_and_tables.pdf")

        # Cleanup extracted images and temporary directory
        temp_image_dir = "temp_extracted_images"
        if os.path.exists(temp_image_dir):
            for file_name in os.listdir(temp_image_dir):
                os.remove(os.path.join(temp_image_dir, file_name))
            os.rmdir(temp_image_dir)
            print("Cleaned up temporary image directory.")

        return send_file(
            pdf_buffer,
            as_attachment=True,
            download_name='images_and_tables.pdf',
            mimetype='application/pdf'
        )
    except Exception as e:
        print("===== /upload-pdfs ERROR =====")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True)
