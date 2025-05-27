from PIL import Image, ImageDraw, ImageFont
import os
from typing import Optional, Tuple, Union

class BingoBoard:
    """
    A simplified class to generate Bingo boards with basic tile images and completion marking.
    """
    
    def __init__(self, size: int = 5, cell_size: int = 150, 
                 background_color: Union[str, Tuple[int, int, int]] = (40, 40, 40), 
                 border_color: Union[str, Tuple[int, int, int]] = (100, 100, 100), 
                 border_width: int = 2):
        """
        Initialize the Bingo board.
        
        Args:
            size: Board dimensions (size x size)
            cell_size: Size of each cell in pixels
            background_color: Background color of cells (default: dark gray)
            border_color: Color of cell borders (default: light gray)
            border_width: Width of cell borders in pixels
        """
        self.size = size
        self.cell_size = cell_size
        self.background_color = background_color
        self.border_color = border_color
        self.border_width = border_width
        
        # Load header image if it exists
        self.header_image = self._load_image("events/data/img/bingo/bingo_header.png")
        
        # Calculate board dimensions
        self.board_width = size * cell_size + (size + 1) * border_width
        self.board_height = size * cell_size + (size + 1) * border_width
        
        # Adjust total height if header exists
        if self.header_image:
            header_height = self.header_image.height
            self.total_height = self.board_height + header_height
        else:
            header_height = 0
            self.total_height = self.board_height
        
        # Initialize the board image with space for header
        self.board = Image.new('RGB', (self.board_width, self.total_height), background_color)
        self.draw = ImageDraw.Draw(self.board)
        
        # Track completion status and cell contents
        self.cell_completion = {}
        self.cell_contents = {}  # Store item IDs for each cell
        
        # Load tile and completion images if they exist
        self.tile_image = self._load_image("events/data/img/bingo/tile.png")
        self.completed_image = self._load_image("events/data/img/bingo/completed.png")
        
        # Load font for footer
        self.font = self._load_font("static/assets/fonts/runescape_uf.ttf", 20)
        
        # Add header if it exists
        if self.header_image:
            self._add_header()
        
        # Draw the initial grid
        self._draw_grid()
        
        # Add footer
        self._add_footer()
    
    def _load_image(self, path: str) -> Optional[Image.Image]:
        """Load an image if it exists, return None if not found."""
        try:
            if os.path.exists(path):
                return Image.open(path)
        except Exception as e:
            print(f"Error loading image {path}: {e}")
        return None
    
    def _load_font(self, path: str, size: int) -> Optional[ImageFont.FreeTypeFont]:
        """Load a font if it exists, return None if not found."""
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        except Exception as e:
            print(f"Error loading font {path}: {e}")
        return None
    
    def _add_header(self):
        """Add the header image centered above the board."""
        if not self.header_image:
            return
            
        # Calculate position to center the header
        header_x = (self.board_width - self.header_image.width) // 2
        header_y = 0
        
        # Paste the header
        if self.header_image.mode == 'RGBA':
            self.board.paste(self.header_image, (header_x, header_y), self.header_image)
        else:
            self.board.paste(self.header_image, (header_x, header_y))
    
    def _add_footer(self):
        """Add the footer text to the bottom right corner."""
        if not self.font:
            return
            
        # Create footer area by extending the board height
        footer_height = 40  # Height for footer area
        new_height = self.total_height + footer_height
        
        # Create new image with extended height
        new_board = Image.new('RGB', (self.board_width, new_height), self.background_color)
        new_board.paste(self.board, (0, 0))
        self.board = new_board
        self.total_height = new_height
        self.draw = ImageDraw.Draw(self.board)
        
        # Footer text elements
        left_text = "Powered by the DropTracker"
        right_text = "View task details: /task {#}"
        
        # Calculate line height
        line_height = self.font.getbbox("Ay")[3] - self.font.getbbox("Ay")[1]
        
        # Calculate starting y position to center text in footer area
        footer_y = self.total_height - footer_height + (footer_height - line_height) // 2
        
        # Draw left text
        left_bbox = self.draw.textbbox((0, 0), left_text, font=self.font)
        left_width = left_bbox[2] - left_bbox[0]
        padding = 10
        left_x = padding
        left_y = footer_y
        
        # Draw right text
        right_bbox = self.draw.textbbox((0, 0), right_text, font=self.font)
        right_width = right_bbox[2] - right_bbox[0]
        right_x = self.board_width - right_width - padding
        right_y = footer_y
        
        # Draw both texts with shadow
        self.draw.text((left_x, left_y), left_text, font=self.font, fill=(200, 200, 200), stroke_width=1, stroke_fill=(0,0,0))
        self.draw.text((right_x, right_y), right_text, font=self.font, fill=(200, 200, 200), stroke_width=1, stroke_fill=(0,0,0))
    
    def _draw_grid(self):
        """Draw the grid lines for the bingo board."""
        # Calculate y offset if header exists
        y_offset = self.header_image.height if self.header_image else 0
        
        # Draw vertical lines
        for i in range(self.size + 1):
            x = i * (self.cell_size + self.border_width)
            self.draw.rectangle([x, y_offset, x + self.border_width, self.total_height], 
                              fill=self.border_color)
        
        # Draw horizontal lines
        for i in range(self.size + 1):
            y = y_offset + i * (self.cell_size + self.border_width)
            self.draw.rectangle([0, y, self.board_width, y + self.border_width], 
                              fill=self.border_color)
    
    def _get_cell_position(self, row: int, col: int) -> Tuple[int, int, int, int]:
        """
        Get the pixel coordinates for a cell.
        
        Args:
            row: Row index (0-based)
            col: Column index (0-based)
            
        Returns:
            Tuple of (left, top, right, bottom) coordinates
        """
        y_offset = self.header_image.height if self.header_image else 0
        left = col * (self.cell_size + self.border_width) + self.border_width
        top = y_offset + row * (self.cell_size + self.border_width) + self.border_width
        right = left + self.cell_size
        bottom = top + self.cell_size
        
        return left, top, right, bottom
    
    def mark_cell_completed(self, row: int, col: int) -> bool:
        """
        Mark a cell as completed with visual indication.
        
        Args:
            row: Row index (0-based)
            col: Column index (0-based)
            
        Returns:
            True if successful, False otherwise
        """
        if not (0 <= row < self.size and 0 <= col < self.size):
            return False
        
        left, top, right, bottom = self._get_cell_position(row, col)
        self.cell_completion[(row, col)] = True
        
        if self.completed_image:
            # Resize completion image to fit cell
            completed = self.completed_image.resize((self.cell_size, self.cell_size))
            # Paste with alpha channel if available
            if completed.mode == 'RGBA':
                self.board.paste(completed, (left, top), completed)
            else:
                self.board.paste(completed, (left, top))
        else:
            # Fallback to semi-transparent green overlay
            overlay = Image.new('RGBA', (right - left, bottom - top), (0, 255, 0, 70))
            # Convert board to RGBA if it isn't already
            if self.board.mode != 'RGBA':
                self.board = self.board.convert('RGBA')
            # Paste with alpha channel
            self.board.paste(overlay, (left, top), overlay)
        
        return True
    
    def unmark_cell_completed(self, row: int, col: int) -> bool:
        """
        Remove completion marking from a cell.
        
        Args:
            row: Row index (0-based)
            col: Column index (0-based)
            
        Returns:
            True if successful, False otherwise
        """
        if not (0 <= row < self.size and 0 <= col < self.size):
            return False
        
        left, top, right, bottom = self._get_cell_position(row, col)
        self.cell_completion[(row, col)] = False
        
        # Redraw cell with tile image or background color
        if self.tile_image:
            tile = self.tile_image.resize((self.cell_size, self.cell_size))
            if tile.mode == 'RGBA':
                self.board.paste(tile, (left, top), tile)
            else:
                self.board.paste(tile, (left, top))
        else:
            self.draw.rectangle([left, top, right, bottom], fill=self.background_color)
        
        return True
    
    def save(self, filename: str) -> bool:
        """
        Save the bingo board image to a file.
        
        Args:
            filename: Output filename
            
        Returns:
            True if successful, False otherwise
        """
        try:
            self.board.save(filename)
            return True
        except Exception as e:
            print(f"Error saving board image: {e}")
            return False
    
    def get_board_image(self) -> Image.Image:
        """
        Get the PIL Image object of the board.
        
        Returns:
            PIL Image object
        """
        return self.board.copy()
    
    def get_completion_stats(self) -> dict:
        """
        Get statistics about board completion.
        
        Returns:
            Dictionary with completion statistics
        """
        total_cells = self.size * self.size
        completed_cells = sum(1 for completed in self.cell_completion.values() if completed)
        
        return {
            'total_cells': total_cells,
            'completed_cells': completed_cells,
            'completion_percentage': (completed_cells / total_cells) * 100 if total_cells > 0 else 0,
            'remaining_cells': total_cells - completed_cells
        }
    

    def set_cell_items_with_extras(self, row: int, col: int, item_ids, task_id: int, text: str, badge: str) -> bool:
        """
        Set a text in a cell.
        """
        if not (0 <= row < self.size and 0 <= col < self.size):
            return False
        self.set_cell_items(row, col, item_ids)
        self.draw_cell_text(row, col, text)
        self.draw_cell_task_id(row, col, task_id)
        self.draw_cell_badge(row, col, badge)

    def draw_cell_task_id(self, row: int, col: int, task_id: int) -> bool:
        """
        Draws a badge-style task ID in the bottom left of the cell.
        The badge has a dark background with white text and a black border.
        """
        if not self.font:
            print("Font not loaded.")
            return False
        if not (0 <= row < self.size and 0 <= col < self.size):
            return False
        left, top, right, bottom = self._get_cell_position(row, col)
        task_id_font_size = int(self.cell_size * 0.10)
        font_path = "static/assets/fonts/runescape_uf.ttf"
        try:
            task_id_font = ImageFont.truetype(font_path, task_id_font_size)
        except Exception:
            task_id_font = self.font  # fallback
            
        # Format task ID with # prefix
        task_id_text = f"#{task_id}"
        
        # Calculate text size
        bbox = task_id_font.getbbox(task_id_text)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        
        # Badge dimensions
        padding_x = int(task_id_font_size * 0.4)
        padding_y = int(task_id_font_size * 0.2)
        badge_width = text_width + 2 * padding_x
        badge_height = text_height + 2 * padding_y
        
        # Position badge in bottom left with margin
        margin = int(self.cell_size * 0.07)
        x0 = left + margin
        y0 = bottom - badge_height - margin
        x1 = left + badge_width + margin
        y1 = bottom - margin
        
        # Draw badge
        draw = ImageDraw.Draw(self.board, 'RGBA')
        badge_color = (33, 37, 41, 255)  # Dark gray/black background
        border_color = (0, 0, 0, 255)
        radius = int(badge_height / 2)
        
        # Draw rounded rectangle
        try:
            draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=badge_color, outline=border_color, width=2)
        except Exception:
            draw.rectangle([x0, y0, x1, y1], fill=badge_color, outline=border_color, width=2)
            
        # Draw text centered in badge
        text_x = x0 + (badge_width - text_width) // 2
        text_y = y0 + (badge_height - text_height) // 2 - bbox[1]  # Adjust for font ascent
        draw.text((text_x, text_y), task_id_text, font=task_id_font, fill=(255,255,255,255), stroke_width=1, stroke_fill=(0,0,0,255))
        return True

    def set_cell_skill_with_extras(self, row: int, col: int, skill_names: list[str], task_id: int, text: str, badge: str) -> bool:
        """
        Set a skill in a cell.
        """
        if not (0 <= row < self.size and 0 <= col < self.size):
            return False
        self.set_cell_skill_grid(row, col, skill_names)
        self.draw_cell_text(row, col, text)
        self.draw_cell_task_id(row, col, task_id)
        self.draw_cell_badge(row, col, badge)
        return True
    
    def set_cell_items(self, row: int, col: int, item_ids) -> bool:
        """
        Set a list of item images in a cell.
        """
        if isinstance(item_ids, list) and isinstance(item_ids[0], list):
            ## Set-based task
            self.set_cell_items_sets(row, col, item_ids)
            return True
        else:
            if not (0 <= row < self.size and 0 <= col < self.size):
                return False
            
            self.set_cell_items_grid(row, col, item_ids)
            
            return True

    def set_cell_item(self, row: int, col: int, item_id: int) -> bool:
        """
        Set an item image in a cell.
        
        Args:
            row: Row index (0-based)
            col: Column index (0-based)
            item_id: ID of the item to display
            
        Returns:
            True if successful, False otherwise
        """
        if not (0 <= row < self.size and 0 <= col < self.size):
            return False
            
        # Load the item image
        item_path = f"static/assets/img/itemdb/{item_id}.png"
        item_image = self._load_image(item_path)
        
        if not item_image:
            print(f"Item image not found: {item_path}")
            return False
            
        # Store the item ID
        self.cell_contents[(row, col)] = item_id
        
        # Get cell position
        left, top, right, bottom = self._get_cell_position(row, col)
        
        # Calculate image size (80% of cell size to leave some padding)
        image_size = int(self.cell_size * 0.8)
        
        # Resize item image while maintaining aspect ratio
        item_image.thumbnail((image_size, image_size), Image.Resampling.LANCZOS)
        
        # Calculate position to center the image in the cell
        x_offset = (self.cell_size - item_image.width) // 2
        y_offset = (self.cell_size - item_image.height) // 2
        
        # Convert board to RGBA if needed
        if self.board.mode != 'RGBA':
            self.board = self.board.convert('RGBA')
            
        # Create a new image for the cell
        cell_image = Image.new('RGBA', (self.cell_size, self.cell_size), (0, 0, 0, 0))
        
        # Paste the item image centered in the cell
        cell_image.paste(item_image, (x_offset, y_offset), item_image if item_image.mode == 'RGBA' else None)
        
        # Paste the cell image onto the board
        self.board.paste(cell_image, (left, top), cell_image)
        
        return True
    
    def get_cell_item(self, row: int, col: int) -> Optional[int]:
        """
        Get the item ID in a cell.
        
        Args:
            row: Row index (0-based)
            col: Column index (0-based)
            
        Returns:
            Item ID if set, None otherwise
        """
        return self.cell_contents.get((row, col))
    
    def clear_cell_item(self, row: int, col: int) -> bool:
        """
        Remove the item image from a cell.
        
        Args:
            row: Row index (0-based)
            col: Column index (0-based)
            
        Returns:
            True if successful, False otherwise
        """
        if not (0 <= row < self.size and 0 <= col < self.size):
            return False
            
        if (row, col) in self.cell_contents:
            del self.cell_contents[(row, col)]
            
            # Redraw the cell with its background
            left, top, right, bottom = self._get_cell_position(row, col)
            self.draw.rectangle([left, top, right, bottom], fill=self.background_color)
            
            # Redraw completion status if needed
            if self.cell_completion.get((row, col)):
                self.mark_cell_completed(row, col)
                
            return True
            
        return False

    def set_cell_skill_grid(self, row: int, col: int, skill_names: list[str]) -> bool:
        """
        Set a list of skill images in a cell, handling both single and multiple skills.
        For multiple skills, arranges them in a grid layout similar to items.
        
        Args:
            row: Row index (0-based)
            col: Column index (0-based)
            skill_names: List of skill names to display
            
        Returns:
            True if successful, False otherwise
        """
        if not (0 <= row < self.size and 0 <= col < self.size):
            return False
            
        self.cell_contents[(row, col)] = skill_names
        left, top, right, bottom = self._get_cell_position(row, col)
        
        if self.board.mode != 'RGBA':
            self.board = self.board.convert('RGBA')
            
        cell_image = Image.new('RGBA', (self.cell_size, self.cell_size), (0, 0, 0, 0))
        
        # Handle single skill case
        if len(skill_names) == 1:
            skill_path = f"static/assets/img/metrics/{skill_names[0]}.png"
            skill_image = self._load_image(skill_path)
            if skill_image is None:
                print(f"Skill image not found: {skill_path}")
                return False
                
            # Convert to RGBA and resize to 80% of cell size
            skill_image = skill_image.convert('RGBA')
            image_size = int(self.cell_size * 0.8)
            skill_image.thumbnail((image_size, image_size), Image.Resampling.LANCZOS)
            
            # Center the image
            x_offset = (self.cell_size - skill_image.width) // 2
            y_offset = (self.cell_size - skill_image.height) // 2
            cell_image.paste(skill_image, (x_offset, y_offset), skill_image)
            self.board.paste(cell_image, (left, top), cell_image)
            return True
            
        # Handle multiple skills
        # Layout constants
        CELL_PADDING = int(self.cell_size * 0.1)
        TOP_PADDING = int(self.cell_size * 0.25)
        SKILLS_PER_ROW = 3  # Fewer skills per row than items since they're typically larger
        available_width = self.cell_size - 2 * CELL_PADDING
        available_height = self.cell_size - TOP_PADDING - CELL_PADDING
        
        # Load all skill images and get their sizes
        images = []
        max_w, max_h = 0, 0
        for skill_name in skill_names:
            skill_path = f"static/assets/img/metrics/{skill_name}.png"
            img = self._load_image(skill_path)
            if img is None:
                print(f"Skill image not found: {skill_path}")
                continue
            img = img.convert('RGBA')
            images.append(img)
            max_w = max(max_w, img.width)
            max_h = max(max_h, img.height)
            
        if not images:
            return False
            
        # Calculate grid layout
        row_count = (len(images) + SKILLS_PER_ROW - 1) // SKILLS_PER_ROW
        
        # Calculate horizontal overlap
        if SKILLS_PER_ROW == 1 or max_w >= available_width:
            x_overlap = 0
        else:
            total_width = max_w + (SKILLS_PER_ROW - 1) * max_w
            if total_width <= available_width:
                x_overlap = 0
            else:
                x_overlap = int((SKILLS_PER_ROW * max_w - available_width) / (SKILLS_PER_ROW - 1))
                x_overlap = min(x_overlap, max_w - 1)
                
        # Calculate vertical overlap
        if row_count == 1 or max_h >= available_height:
            y_overlap = 0
        else:
            total_height = max_h + (row_count - 1) * max_h
            if total_height <= available_height:
                y_overlap = 0
            else:
                y_overlap = int((row_count * max_h - available_height) / (row_count - 1))
                y_overlap = min(y_overlap, max_h - 1)
                
        # Draw images in grid
        for idx, img in enumerate(images):
            row_idx = idx // SKILLS_PER_ROW
            col_idx = idx % SKILLS_PER_ROW
            
            x = CELL_PADDING + col_idx * (max_w - x_overlap)
            y = TOP_PADDING + row_idx * (max_h - y_overlap)
            
            # Don't draw outside the cell
            if x + img.width > self.cell_size:
                x = self.cell_size - img.width
            if y + img.height > self.cell_size:
                y = self.cell_size - img.height
                
            cell_image.paste(img, (x, y), img)
            
        self.board.paste(cell_image, (left, top), cell_image)
        return True

    def set_cell_items_grid(self, row: int, col: int, item_ids: list[int]) -> bool:
        """
        Set multiple item images in a cell, stacking each item in rows of 4, overlapping horizontally (no resizing),
        and if needed, overlap rows vertically so all items fit. Keeps top padding for text.
        """
        if not (0 <= row < self.size and 0 <= col < self.size):
            return False
        self.cell_contents[(row, col)] = item_ids
        left, top, right, bottom = self._get_cell_position(row, col)
        if self.board.mode != 'RGBA':
            self.board = self.board.convert('RGBA')
        cell_image = Image.new('RGBA', (self.cell_size, self.cell_size), (0, 0, 0, 0))
        num_items = len(item_ids)
        if num_items == 0:
            return True
        elif num_items == 1:
            return self.set_cell_item(row, col, item_ids[0])
        # Layout constants
        CELL_PADDING = int(self.cell_size * 0.1)
        TOP_PADDING = int(self.cell_size * 0.25)
        ITEMS_PER_ROW = 4
        available_width = self.cell_size - 2 * CELL_PADDING
        available_height = self.cell_size - TOP_PADDING - CELL_PADDING
        # Load all images and get their sizes
        images = []
        max_w, max_h = 0, 0
        for item_id in item_ids:
            item_path = f"static/assets/img/itemdb/{item_id}.png"
            img = self._load_image(item_path)
            if img is None:
                print(f"Item image not found: {item_path}")
                continue
            img = img.convert('RGBA')
            images.append(img)
            max_w = max(max_w, img.width)
            max_h = max(max_h, img.height)
        if not images:
            return False
        # Use max_w, max_h for all items for consistent stacking
        row_count = (len(images) + ITEMS_PER_ROW - 1) // ITEMS_PER_ROW
        # Horizontal overlap calculation
        if ITEMS_PER_ROW == 1 or max_w >= available_width:
            x_overlap = 0
        else:
            total_width = max_w + (ITEMS_PER_ROW - 1) * max_w
            if total_width <= available_width:
                x_overlap = 0
            else:
                x_overlap = int((ITEMS_PER_ROW * max_w - available_width) / (ITEMS_PER_ROW - 1))
                x_overlap = min(x_overlap, max_w - 1)
        # Vertical overlap calculation
        if row_count == 1 or max_h >= available_height:
            y_overlap = 0
        else:
            total_height = max_h + (row_count - 1) * max_h
            if total_height <= available_height:
                y_overlap = 0
            else:
                y_overlap = int((row_count * max_h - available_height) / (row_count - 1))
                y_overlap = min(y_overlap, max_h - 1)
        # Draw images
        for idx, img in enumerate(images):
            row_idx = idx // ITEMS_PER_ROW
            col_idx = idx % ITEMS_PER_ROW
            x = CELL_PADDING + col_idx * (max_w - x_overlap)
            y = TOP_PADDING + row_idx * (max_h - y_overlap)
            # Don't draw outside the cell
            if x + img.width > self.cell_size:
                x = self.cell_size - img.width
            if y + img.height > self.cell_size:
                y = self.cell_size - img.height
            cell_image.paste(img, (x, y), img)
        self.board.paste(cell_image, (left, top), cell_image)
        return True

    def draw_cell_text(self, row: int, col: int, text: str) -> bool:
        """
        Draws text at the top of the cell, using the RS font, with a 1px black stroke and yellow color.
        Always prints the entire string, line-breaking as needed, even if it overlaps the item images.
        """
        if not self.font:
            print("Font not loaded.")
            return False
        if not (0 <= row < self.size and 0 <= col < self.size):
            return False
        left, top, right, bottom = self._get_cell_position(row, col)
        CELL_PADDING = int(self.cell_size * 0.1)
        TOP_PADDING = int(self.cell_size * 0.25)
        max_width = self.cell_size - 2 * CELL_PADDING
        # Word wrap
        words = text.split()
        lines = []
        current_line = ""
        for word in words:
            test_line = current_line + (" " if current_line else "") + word
            bbox = self.font.getbbox(test_line)
            width = bbox[2] - bbox[0]
            if width > max_width and current_line:
                lines.append(current_line)
                current_line = word
            else:
                current_line = test_line
        if current_line:
            lines.append(current_line)
        # Draw each line centered, even if it overlaps items
        line_height = self.font.getbbox("Ay")[3] - self.font.getbbox("Ay")[1]
        draw = ImageDraw.Draw(self.board)
        for i, line in enumerate(lines):
            bbox = self.font.getbbox(line)
            text_width = bbox[2] - bbox[0]
            x = left + (self.cell_size - text_width) // 2
            y = top + CELL_PADDING + i * line_height
            # Draw yellow text
            draw.text((x,y), line, font=self.font,
              fill=(255,220,40), stroke_width=1, stroke_fill=(0,0,0))
        return True

    def draw_free_space_tile(self):
        """
        Draws 'FREE SPACE' in the center cell, using a larger font size and the same yellow/stroke style,
        and marks the cell as completed by default. The text is split into two lines.
        """
        center = self.size // 2
        row, col = center, center
        left, top, right, bottom = self._get_cell_position(row, col)
        # Use a larger font size for the free space
        font_path = "static/assets/fonts/runescape_uf.ttf"
        large_font_size = int(self.cell_size * 0.28)
        try:
            large_font = ImageFont.truetype(font_path, large_font_size)
        except Exception:
            large_font = self.font  # fallback
        lines = ["FREE", "SPACE"]
        # Calculate text sizes
        bboxes = [large_font.getbbox(line) for line in lines]
        text_widths = [bbox[2] - bbox[0] for bbox in bboxes]
        text_heights = [bbox[3] - bbox[1] for bbox in bboxes]
        gap = int(large_font_size * 0.1)
        total_height = text_heights[0] + text_heights[1] + gap
        # Center block vertically and horizontally
        y_start = top + (self.cell_size - total_height) // 2
        self.mark_cell_completed(row, col)
        draw = ImageDraw.Draw(self.board)
        for i, line in enumerate(lines):
            text_width = text_widths[i]
            text_height = text_heights[i]
            x = left + (self.cell_size - text_width) // 2
            y = y_start + sum(text_heights[:i]) + (gap if i == 1 else 0)
            # Draw yellow text
            draw.text((x, y), line, font=large_font, fill=(255, 220, 40), stroke_width=2, stroke_fill=(0,0,0))
        # Mark the cell as completed
        return True

    def draw_cell_badge(self, row: int, col: int, text: str) -> bool:
        """
        Draws a Bootstrap-esque badge with the given text in the bottom right of the cell.
        The badge background is solid, and the text is semi-transparent white and vertically centered.
        """
        if not self.font:
            print("Font not loaded.")
            return False
        if not (0 <= row < self.size and 0 <= col < self.size):
            return False
        left, top, right, bottom = self._get_cell_position(row, col)
        badge_font_size = int(self.cell_size * 0.10)
        font_path = "static/assets/fonts/runescape_uf.ttf"
        try:
            badge_font = ImageFont.truetype(font_path, badge_font_size)
        except Exception:
            badge_font = self.font  # fallback
        # Calculate text size
        bbox = badge_font.getbbox(text)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        padding_x = int(badge_font_size * 0.6)
        padding_y = int(badge_font_size * 0.3)
        badge_width = text_width + 2 * padding_x
        badge_height = text_height + 2 * padding_y
        # Position badge in bottom right with margin
        margin = int(self.cell_size * 0.07)
        x0 = right - badge_width - margin
        y0 = bottom - badge_height - margin
        x1 = right - margin
        y1 = bottom - margin
        # Draw badge
        draw = ImageDraw.Draw(self.board, 'RGBA')
        badge_color = (255, 193, 7, 255)  # Bootstrap yellow, solid
        border_color = (0, 0, 0, 255)
        radius = int(badge_height / 2)
        # Draw rounded rectangle (Pillow >= 8.2.0)
        try:
            draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=badge_color, outline=border_color, width=2)
        except Exception:
            draw.rectangle([x0, y0, x1, y1], fill=badge_color, outline=border_color, width=2)
        # Draw text centered in badge
        text_x = x0 + (badge_width - text_width) // 2
        text_y = y0 + (badge_height - text_height) // 2 - bbox[1]  # Adjust for font ascent
        draw.text((text_x, text_y), text, font=badge_font, fill=(255,255,255,180), stroke_width=1, stroke_fill=(0,0,0,180))
        return True

    def set_cell_items_sets(self, row: int, col: int, item_sets: list[list[int]]) -> bool:
        """
        Set multiple sets of item images in a cell, where each set is displayed on its own line.
        Each set is treated as a row of items, with items within a set displayed horizontally.
        
        Args:
            row: Row index (0-based)
            col: Column index (0-based)
            item_sets: List of lists, where each inner list contains item IDs for that row
            
        Returns:
            True if successful, False otherwise
        """
        if not (0 <= row < self.size and 0 <= col < self.size):
            return False
            
        self.cell_contents[(row, col)] = item_sets
        left, top, right, bottom = self._get_cell_position(row, col)
        
        if self.board.mode != 'RGBA':
            self.board = self.board.convert('RGBA')
            
        cell_image = Image.new('RGBA', (self.cell_size, self.cell_size), (0, 0, 0, 0))
        
        # Layout constants
        CELL_PADDING = int(self.cell_size * 0.1)
        TOP_PADDING = int(self.cell_size * 0.25)
        available_width = self.cell_size - 2 * CELL_PADDING
        available_height = self.cell_size - TOP_PADDING - CELL_PADDING
        
        # Calculate height per set
        height_per_set = available_height // len(item_sets)
        
        # Process each set (row) of items
        for set_idx, item_set in enumerate(item_sets):
            if not item_set:  # Skip empty sets
                continue
                
            # Load all images for this set
            images = []
            max_w, max_h = 0, 0
            for item_id in item_set:
                item_path = f"static/assets/img/itemdb/{item_id}.png"
                img = self._load_image(item_path)
                if img is None:
                    print(f"Item image not found: {item_path}")
                    continue
                img = img.convert('RGBA')
                images.append(img)
                max_w = max(max_w, img.width)
                max_h = max(max_h, img.height)
                
            if not images:
                continue
                
            # Calculate horizontal overlap for this set
            if len(images) == 1 or max_w >= available_width:
                x_overlap = 0
            else:
                total_width = max_w + (len(images) - 1) * max_w
                if total_width <= available_width:
                    x_overlap = 0
                else:
                    x_overlap = int((len(images) * max_w - available_width) / (len(images) - 1))
                    x_overlap = min(x_overlap, max_w - 1)
                    
            # Calculate vertical position for this set
            y_start = TOP_PADDING + set_idx * height_per_set
            
            # Draw images for this set
            for idx, img in enumerate(images):
                x = CELL_PADDING + idx * (max_w - x_overlap)
                y = y_start + (height_per_set - img.height) // 2
                
                # Don't draw outside the cell
                if x + img.width > self.cell_size:
                    x = self.cell_size - img.width
                if y + img.height > self.cell_size:
                    y = self.cell_size - img.height
                    
                cell_image.paste(img, (x, y), img)
                
        self.board.paste(cell_image, (left, top), cell_image)
        return True

    def set_cell_npc_with_extras(self, row: int, col: int, npc_ids: list[int], task_id: int, text: str, badge: str) -> bool:
        """
        Set multiple NPCs in a cell with additional elements (text, task ID, badge).
        
        Args:
            row: Row index (0-based)
            col: Column index (0-based)
            npc_ids: List of NPC IDs to display
            task_id: Task ID to display in badge
            text: Text to display at top of cell
            badge: Badge text to display
            
        Returns:
            True if successful, False otherwise
        """
        if not (0 <= row < self.size and 0 <= col < self.size):
            return False
        self.set_cell_npc_grid(row, col, npc_ids)
        self.draw_cell_text(row, col, text)
        self.draw_cell_task_id(row, col, task_id)
        self.draw_cell_badge(row, col, badge)
        return True

    def set_cell_npc_grid(self, row: int, col: int, npc_ids: list[int]) -> bool:
        """
        Set multiple NPC images in a cell, arranging them in a grid layout.
        
        Args:
            row: Row index (0-based)
            col: Column index (0-based)
            npc_ids: List of NPC IDs to display
            
        Returns:
            True if successful, False otherwise
        """
        if not (0 <= row < self.size and 0 <= col < self.size):
            return False
            
        self.cell_contents[(row, col)] = npc_ids
        left, top, right, bottom = self._get_cell_position(row, col)
        
        if self.board.mode != 'RGBA':
            self.board = self.board.convert('RGBA')
            
        cell_image = Image.new('RGBA', (self.cell_size, self.cell_size), (0, 0, 0, 0))
        
        # Handle single NPC case
        if len(npc_ids) == 1:
            npc_path = f"static/assets/img/npcdb/{npc_ids[0]}.png"
            npc_image = self._load_image(npc_path)
            if npc_image is None:
                print(f"NPC image not found: {npc_path}")
                return False
                
            # Convert to RGBA and resize to 80% of cell size
            npc_image = npc_image.convert('RGBA')
            image_size = int(self.cell_size * 0.6)
            npc_image.thumbnail((image_size, image_size), Image.Resampling.LANCZOS)
            
            # Center the image
            x_offset = (self.cell_size - npc_image.width) // 2
            y_offset = (self.cell_size - npc_image.height) // 2
            cell_image.paste(npc_image, (x_offset, y_offset), npc_image)
            self.board.paste(cell_image, (left, top), cell_image)
            return True
            
        # Handle multiple NPCs
        # Layout constants
        CELL_PADDING = int(self.cell_size * 0.1)
        TOP_PADDING = int(self.cell_size * 0.25)
        NPCS_PER_ROW = 3  # Fewer NPCs per row since they're typically larger
        available_width = self.cell_size - 2 * CELL_PADDING
        available_height = self.cell_size - TOP_PADDING - CELL_PADDING
        
        # Load all NPC images and get their sizes
        images = []
        max_w, max_h = 0, 0
        for npc_id in npc_ids:
            npc_path = f"static/assets/img/npcdb/{npc_id}.png"
            img = self._load_image(npc_path)
            if img is None:
                print(f"NPC image not found: {npc_path}")
                continue
            img = img.convert('RGBA')
            images.append(img)
            max_w = max(max_w, img.width)
            max_h = max(max_h, img.height)
            
        if not images:
            return False
            
        # Calculate grid layout
        row_count = (len(images) + NPCS_PER_ROW - 1) // NPCS_PER_ROW
        
        # Calculate horizontal overlap
        if NPCS_PER_ROW == 1 or max_w >= available_width:
            x_overlap = 0
        else:
            total_width = max_w + (NPCS_PER_ROW - 1) * max_w
            if total_width <= available_width:
                x_overlap = 0
            else:
                x_overlap = int((NPCS_PER_ROW * max_w - available_width) / (NPCS_PER_ROW - 1))
                x_overlap = min(x_overlap, max_w - 1)
                
        # Calculate vertical overlap
        if row_count == 1 or max_h >= available_height:
            y_overlap = 0
        else:
            total_height = max_h + (row_count - 1) * max_h
            if total_height <= available_height:
                y_overlap = 0
            else:
                y_overlap = int((row_count * max_h - available_height) / (row_count - 1))
                y_overlap = min(y_overlap, max_h - 1)
                
        # Draw images in grid
        for idx, img in enumerate(images):
            row_idx = idx // NPCS_PER_ROW
            col_idx = idx % NPCS_PER_ROW
            
            x = CELL_PADDING + col_idx * (max_w - x_overlap)
            y = TOP_PADDING + row_idx * (max_h - y_overlap)
            
            # Don't draw outside the cell
            if x + img.width > self.cell_size:
                x = self.cell_size - img.width
            if y + img.height > self.cell_size:
                y = self.cell_size - img.height
                
            cell_image.paste(img, (x, y), img)
            
        self.board.paste(cell_image, (left, top), cell_image)
        return True

    def set_cell_npc_gp_target(self, row: int, col: int, npc_ids: list[int], task_id: int, text: str, badge: str) -> bool:
        """
        Set multiple NPC images and a coin stack in a cell for GP target tasks.
        
        Args:
            row: Row index (0-based)
            col: Column index (0-based)
            npc_ids: List of NPC IDs to display
            task_id: Task ID to display in badge
            text: Text to display at top of cell
            badge: Badge text to display
            
        Returns:
            True if successful, False otherwise
        """
        if not (0 <= row < self.size and 0 <= col < self.size):
            return False
            
        # Load all NPC images
        npc_images = []
        for npc_id in npc_ids:
            npc_path = f"static/assets/img/npcdb/{npc_id}.png"
            npc_image = self._load_image(npc_path)
            if not npc_image:
                print(f"NPC image not found: {npc_path}")
                continue
            npc_images.append(npc_image)
            
        if not npc_images:
            return False
            
        # Load the coin stack image
        coin_path = "static/assets/img/itemdb/1004.png"
        coin_image = self._load_image(coin_path)
        
        if not coin_image:
            print(f"Coin image not found: {coin_path}")
            return False
            
        # Store the NPC IDs and coin ID
        self.cell_contents[(row, col)] = (npc_ids, 1004)
        
        # Get cell position
        left, top, right, bottom = self._get_cell_position(row, col)
        
        # Calculate image sizes
        if len(npc_images) == 1:
            npc_size = int(self.cell_size * 0.7)
            coin_size = int(self.cell_size * 0.5)
        else:
            npc_size = int(self.cell_size * 0.5)  # Smaller NPCs when multiple
            coin_size = int(self.cell_size * 0.4)  # Smaller coin stack
            
        # Resize images while maintaining aspect ratio
        for i in range(len(npc_images)):
            npc_images[i] = npc_images[i].convert('RGBA')
            npc_images[i].thumbnail((npc_size, npc_size), Image.Resampling.LANCZOS)
        coin_image = coin_image.convert('RGBA')
        coin_image.thumbnail((coin_size, coin_size), Image.Resampling.LANCZOS)
        
        # Convert board to RGBA if needed
        if self.board.mode != 'RGBA':
            self.board = self.board.convert('RGBA')
            
        # Create a new image for the cell
        cell_image = Image.new('RGBA', (self.cell_size, self.cell_size), (0, 0, 0, 0))
        
        if len(npc_images) == 1:
            # Single NPC case - place NPC and coin side by side
            total_width = npc_images[0].width + coin_image.width
            spacing = int(self.cell_size * 0.1)
            x_offset = (self.cell_size - total_width - spacing) // 2
            y_offset = (self.cell_size - max(npc_images[0].height, coin_image.height)) // 2
            
            # Paste the NPC image
            cell_image.paste(npc_images[0], (x_offset, y_offset), npc_images[0])
            
            # Paste the coin image
            coin_x = x_offset + npc_images[0].width + spacing
            coin_y = y_offset + (npc_images[0].height - coin_image.height) // 2
            cell_image.paste(coin_image, (coin_x, coin_y), coin_image)
        else:
            # Multiple NPCs case - arrange in grid with coin stack
            CELL_PADDING = int(self.cell_size * 0.1)
            TOP_PADDING = int(self.cell_size * 0.25)
            NPCS_PER_ROW = 2  # Fewer NPCs per row to leave space for coin stack
            available_width = self.cell_size - 2 * CELL_PADDING - coin_image.width - CELL_PADDING
            available_height = self.cell_size - TOP_PADDING - CELL_PADDING
            
            # Calculate grid layout
            row_count = (len(npc_images) + NPCS_PER_ROW - 1) // NPCS_PER_ROW
            
            # Calculate overlaps
            max_w = max(img.width for img in npc_images)
            max_h = max(img.height for img in npc_images)
            
            if NPCS_PER_ROW == 1 or max_w >= available_width:
                x_overlap = 0
            else:
                total_width = max_w + (NPCS_PER_ROW - 1) * max_w
                if total_width <= available_width:
                    x_overlap = 0
                else:
                    x_overlap = int((NPCS_PER_ROW * max_w - available_width) / (NPCS_PER_ROW - 1))
                    x_overlap = min(x_overlap, max_w - 1)
                    
            if row_count == 1 or max_h >= available_height:
                y_overlap = 0
            else:
                total_height = max_h + (row_count - 1) * max_h
                if total_height <= available_height:
                    y_overlap = 0
                else:
                    y_overlap = int((row_count * max_h - available_height) / (row_count - 1))
                    y_overlap = min(y_overlap, max_h - 1)
                    
            # Draw NPCs in grid
            for idx, img in enumerate(npc_images):
                row_idx = idx // NPCS_PER_ROW
                col_idx = idx % NPCS_PER_ROW
                
                x = CELL_PADDING + col_idx * (max_w - x_overlap)
                y = TOP_PADDING + row_idx * (max_h - y_overlap)
                
                # Don't draw outside the cell
                if x + img.width > self.cell_size - coin_image.width - CELL_PADDING:
                    x = self.cell_size - coin_image.width - CELL_PADDING - img.width
                if y + img.height > self.cell_size:
                    y = self.cell_size - img.height
                    
                cell_image.paste(img, (x, y), img)
                
            # Place coin stack on the right
            coin_x = self.cell_size - coin_image.width - CELL_PADDING
            coin_y = (self.cell_size - coin_image.height) // 2
            cell_image.paste(coin_image, (coin_x, coin_y), coin_image)
            
        # Paste the cell image onto the board
        self.board.paste(cell_image, (left, top), cell_image)
        
        # Add text and badges
        self.draw_cell_text(row, col, text)
        self.draw_cell_task_id(row, col, task_id)
        self.draw_cell_badge(row, col, badge)
        
        return True
