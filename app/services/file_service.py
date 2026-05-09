from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import pandas as pd

from app.utils.logger import get_logger

logger = get_logger("file_service")


@dataclass
class ValidationResult:
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    missing_columns: List[str] = field(default_factory=list)
    row_count: int = 0


class FileService:
    """Handles reading, validating, and managing input data files."""

    # -----------------------------------------------------------------------
    # Reading
    # -----------------------------------------------------------------------

    def read_csv(self, path: str) -> pd.DataFrame:
        """
        Read a CSV file into a DataFrame.

        Attempts common encodings (utf-8, latin-1, cp1252) and separators
        (comma, semicolon, tab) automatically.
        """
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"CSV file not found: {path}")

        encodings = ["utf-8", "utf-8-sig", "latin-1", "cp1252"]
        separators = [",", ";", "\t", "|"]

        last_exc: Optional[Exception] = None
        for enc in encodings:
            for sep in separators:
                try:
                    df = pd.read_csv(path, encoding=enc, sep=sep, engine="python", on_bad_lines="warn")
                    if df.shape[1] > 1:  # at least 2 columns → correct separator
                        logger.info(f"read_csv: {file_path.name} ({len(df)} rows, enc={enc}, sep={sep!r})")
                        return df
                except Exception as exc:
                    last_exc = exc
                    continue

        raise ValueError(f"Could not parse CSV {path}: {last_exc}") from last_exc

    def read_excel(self, path: str, sheet_name: str | int = 0) -> pd.DataFrame:
        """Read an Excel file (.xls / .xlsx) using openpyxl."""
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Excel file not found: {path}")

        try:
            df = pd.read_excel(path, sheet_name=sheet_name, engine="openpyxl")
            logger.info(f"read_excel: {file_path.name} sheet={sheet_name!r} ({len(df)} rows)")
            return df
        except Exception as exc:
            raise ValueError(f"Could not read Excel {path}: {exc}") from exc

    def auto_read(self, path: str) -> pd.DataFrame:
        """
        Detect the file type by extension and read it accordingly.

        Supports .csv, .xlsx, .xls.
        """
        suffix = Path(path).suffix.lower()
        if suffix == ".csv":
            return self.read_csv(path)
        elif suffix in (".xlsx", ".xls", ".xlsm"):
            return self.read_excel(path)
        else:
            raise ValueError(f"Unsupported file type: {suffix}. Expected .csv or .xlsx")

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    def validate_schema(self, df: pd.DataFrame, required_columns: List[str]) -> ValidationResult:
        """
        Check that all *required_columns* are present in *df*.

        Also checks for completely-empty required columns and very low row counts.
        """
        errors: List[str] = []
        warnings: List[str] = []
        missing: List[str] = []

        # Normalise column names for comparison (lowercase, strip whitespace)
        df_cols_normalized = {c.strip().lower(): c for c in df.columns}

        for req_col in required_columns:
            normalised = req_col.strip().lower()
            if normalised not in df_cols_normalized:
                missing.append(req_col)
                errors.append(f"Required column missing: '{req_col}'")
            else:
                actual_col = df_cols_normalized[normalised]
                null_pct = df[actual_col].isna().mean() * 100
                if null_pct == 100:
                    errors.append(f"Column '{req_col}' is entirely empty")
                elif null_pct > 50:
                    warnings.append(f"Column '{req_col}' is {null_pct:.0f}% null")

        if len(df) == 0:
            errors.append("DataFrame has zero rows")
        elif len(df) < 3:
            warnings.append(f"Very few rows: {len(df)}")

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            missing_columns=missing,
            row_count=len(df),
        )

    # -----------------------------------------------------------------------
    # Conversion
    # -----------------------------------------------------------------------

    def to_entry_records(self, df: pd.DataFrame, source_file: str) -> List[dict]:
        """
        Convert each row of *df* into a DataEntry payload dict.

        Each record has the shape::

            {
                "source_file": str,
                "row_number": int,        # 1-indexed
                "data_payload": dict,     # row values, NaN → None
            }
        """
        records: List[dict] = []
        for idx, (_, row) in enumerate(df.iterrows(), start=1):
            payload: dict = {}
            for col, val in row.items():
                # Normalise column names to snake_case
                key = str(col).strip().lower().replace(" ", "_").replace("-", "_")
                if pd.isna(val):
                    payload[key] = None
                elif hasattr(val, "isoformat"):
                    payload[key] = val.isoformat()
                else:
                    payload[key] = val
            records.append({
                "source_file": source_file,
                "row_number": idx,
                "data_payload": payload,
            })
        logger.info(f"to_entry_records: {len(records)} records from {Path(source_file).name}")
        return records

    # -----------------------------------------------------------------------
    # File management
    # -----------------------------------------------------------------------

    def mark_file_processed(self, path: str) -> str:
        """
        Move the file to a ``processed/`` subdirectory next to the original.

        Returns the new file path.
        """
        src = Path(path)
        if not src.exists():
            logger.warning(f"mark_file_processed: file not found: {path}")
            return path

        processed_dir = src.parent / "processed"
        processed_dir.mkdir(parents=True, exist_ok=True)
        dest = processed_dir / src.name

        # Avoid name collision
        counter = 1
        while dest.exists():
            dest = processed_dir / f"{src.stem}_{counter}{src.suffix}"
            counter += 1

        shutil.move(str(src), str(dest))
        logger.info(f"mark_file_processed: moved {src.name} → {dest}")
        return str(dest)
