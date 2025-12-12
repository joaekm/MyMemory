"""
DateService - Central datumhantering för MyMemory (OBJEKT-50)

Prioritetsordning för datumextraktion:
1. Frontmatter (timestamp_created) - mest pålitligt
2. Filnamn (Slack_kanal_2025-12-11_uuid.txt) - pålitligt för Slack
3. PDF-metadata (CreationDate) - för PDF-filer
4. Filsystem (birthtime/mtime) - alltid försöker sist

HARDFAIL om inget fungerar - fil ska då flyttas till Failed-mappen.

Användning:
    from services.utils.date_service import get_date, get_timestamp
    
    date_str = get_date(filepath)        # "2025-12-11"
    timestamp = get_timestamp(filepath)  # datetime object
"""

import os
import re
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

LOGGER = logging.getLogger('DateService')

# === ABSTRACT BASE ===

class DateExtractor(ABC):
    """Abstrakt basklass för datumextraktion."""
    
    @abstractmethod
    def can_extract(self, filepath: str) -> bool:
        """Returnerar True om denna extractor kan hantera filen."""
        pass
    
    @abstractmethod
    def extract(self, filepath: str) -> Optional[datetime]:
        """Extraherar datum, returnerar None om det inte går."""
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Namn på extractorn för loggning."""
        pass


# === EXTRACTORS ===

class FrontmatterExtractor(DateExtractor):
    """
    Läser timestamp_created från YAML frontmatter.
    
    Förväntat format i .md-filer:
    ---
    timestamp_created: '2025-12-11T14:30:00+01:00'
    ---
    
    Validerar att datumet är rimligt (>= MIN_YEAR).
    """
    
    PATTERN = re.compile(r"timestamp_created:\s*['\"]?([^'\"\n]+)")
    MIN_YEAR = 2015  # Datum äldre än detta anses korrupt
    
    @property
    def name(self) -> str:
        return "frontmatter"
    
    def can_extract(self, filepath: str) -> bool:
        return filepath.lower().endswith('.md')
    
    def extract(self, filepath: str) -> Optional[datetime]:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                # Läs bara första 2000 tecken (frontmatter är i början)
                content = f.read(2000)
            
            match = self.PATTERN.search(content)
            if match:
                ts_str = match.group(1).strip()
                result = None
                
                # Försök parsa ISO-format
                # Hantera både med och utan timezone
                for fmt in [
                    '%Y-%m-%dT%H:%M:%S.%f%z',
                    '%Y-%m-%dT%H:%M:%S%z',
                    '%Y-%m-%dT%H:%M:%S.%f',
                    '%Y-%m-%dT%H:%M:%S',
                    '%Y-%m-%d'
                ]:
                    try:
                        result = datetime.strptime(ts_str[:26].replace('+02:00', '+0200').replace('+01:00', '+0100'), fmt)
                        break
                    except ValueError:
                        # Förväntat: prova nästa format
                        LOGGER.debug(f"Format {fmt} matchade inte för {ts_str[:20]}")
                        continue
                
                # Fallback: försök fromisoformat
                if not result:
                    try:
                        result = datetime.fromisoformat(ts_str)
                    except ValueError:
                        LOGGER.debug(f"fromisoformat misslyckades för {ts_str[:20]}")
                
                # Validera att datumet är rimligt
                if result and result.year >= self.MIN_YEAR:
                    return result
                elif result:
                    LOGGER.debug(f"Frontmatter-datum {result.year} för gammalt i {os.path.basename(filepath)}")
                    
        except Exception as e:
            LOGGER.debug(f"FrontmatterExtractor misslyckades för {filepath}: {e}")
        
        return None


class SlackFilenameExtractor(DateExtractor):
    """
    Extraherar datum från Slack-filnamn.
    
    Format: Slack_kanal_2025-12-11_uuid.txt
            Slack_se_drive_2025-12-11_uuid.txt (kanal med underscore)
    """
    
    # Matcha datum-mönstret var som helst i filnamnet
    PATTERN = re.compile(r'Slack_.+?_(\d{4}-\d{2}-\d{2})_[0-9a-f]{8}-')
    
    @property
    def name(self) -> str:
        return "slack_filename"
    
    def can_extract(self, filepath: str) -> bool:
        basename = os.path.basename(filepath)
        return basename.startswith('Slack_')
    
    def extract(self, filepath: str) -> Optional[datetime]:
        basename = os.path.basename(filepath)
        match = self.PATTERN.search(basename)
        if match:
            try:
                return datetime.strptime(match.group(1), '%Y-%m-%d')
            except ValueError:
                LOGGER.debug(f"Kunde inte parsa Slack-datum från {basename}")
        return None


class PDFExtractor(DateExtractor):
    """
    Läser CreationDate från PDF-metadata.
    
    Kräver: pypdf
    """
    
    @property
    def name(self) -> str:
        return "pdf_metadata"
    
    def can_extract(self, filepath: str) -> bool:
        return filepath.lower().endswith('.pdf')
    
    def extract(self, filepath: str) -> Optional[datetime]:
        try:
            from pypdf import PdfReader
            reader = PdfReader(filepath)
            
            if reader.metadata:
                # Försök creation_date först
                if hasattr(reader.metadata, 'creation_date') and reader.metadata.creation_date:
                    cd = reader.metadata.creation_date
                    if isinstance(cd, datetime):
                        return cd
                
                # Försök /CreationDate som sträng
                creation_date_str = reader.metadata.get('/CreationDate')
                if creation_date_str:
                    # PDF-format: D:YYYYMMDDHHmmSS+TZ
                    # Exempel: D:20231215143052+01'00'
                    if creation_date_str.startswith('D:'):
                        date_part = creation_date_str[2:16]  # YYYYMMDDHHMMSS
                        try:
                            return datetime.strptime(date_part, '%Y%m%d%H%M%S')
                        except ValueError:
                            LOGGER.debug(f"Kunde inte parsa PDF-datum med tid: {date_part}")
                            # Försök bara datum
                            try:
                                return datetime.strptime(date_part[:8], '%Y%m%d')
                            except ValueError:
                                LOGGER.debug(f"Kunde inte parsa PDF-datum: {date_part[:8]}")
                                
        except ImportError:
            LOGGER.warning("pypdf inte installerat - kan inte läsa PDF-metadata")
        except Exception as e:
            LOGGER.debug(f"PDFExtractor misslyckades för {filepath}: {e}")
        
        return None


class FilesystemExtractor(DateExtractor):
    """
    Fallback: använder filsystemets metadata.
    
    Prioritet:
    1. st_birthtime (om tillgängligt och rimligt)
    2. st_mtime (modifieringsdatum)
    
    Validering: Datum äldre än MIN_YEAR anses korrupt.
    """
    
    MIN_YEAR = 2015  # Äldre anses korrupt (1984-datum etc.)
    
    @property
    def name(self) -> str:
        return "filesystem"
    
    def can_extract(self, filepath: str) -> bool:
        return os.path.exists(filepath)
    
    def extract(self, filepath: str) -> Optional[datetime]:
        try:
            stat = os.stat(filepath)
            
            # Försök birthtime först (macOS)
            if hasattr(stat, 'st_birthtime'):
                birthtime = datetime.fromtimestamp(stat.st_birthtime)
                if birthtime.year >= self.MIN_YEAR:
                    return birthtime
                else:
                    LOGGER.debug(f"birthtime {birthtime.year} för gammal, använder mtime")
            
            # Fallback till mtime
            mtime = datetime.fromtimestamp(stat.st_mtime)
            if mtime.year >= self.MIN_YEAR:
                return mtime
            
            # Även mtime är korrupt
            LOGGER.warning(f"Både birthtime och mtime är korrupta för {filepath}")
            return None
            
        except Exception as e:
            LOGGER.debug(f"FilesystemExtractor misslyckades för {filepath}: {e}")
        
        return None


# === PRIORITERAD LISTA AV EXTRACTORS ===

EXTRACTORS = [
    FrontmatterExtractor(),
    SlackFilenameExtractor(),
    PDFExtractor(),
    FilesystemExtractor(),  # Alltid sist som fallback
]


# === HUVUDFUNKTIONER ===

def get_timestamp(filepath: str) -> datetime:
    """
    Hämta timestamp för en fil.
    
    Försöker alla extractors i prioritetsordning tills en lyckas.
    
    Args:
        filepath: Sökväg till filen
        
    Returns:
        datetime-objekt
        
    Raises:
        RuntimeError: HARDFAIL_DATE om inget datum kan extraheras
    """
    for extractor in EXTRACTORS:
        if extractor.can_extract(filepath):
            result = extractor.extract(filepath)
            if result:
                LOGGER.debug(f"Datum från {extractor.name}: {result} för {os.path.basename(filepath)}")
                return result
    
    raise RuntimeError(f"HARDFAIL_DATE: Kunde inte extrahera datum från {filepath}")


def get_date(filepath: str) -> str:
    """
    Hämta datum som YYYY-MM-DD sträng.
    
    Args:
        filepath: Sökväg till filen
        
    Returns:
        Datum som "YYYY-MM-DD" sträng
        
    Raises:
        RuntimeError: HARDFAIL_DATE om inget datum kan extraheras
    """
    return get_timestamp(filepath).strftime('%Y-%m-%d')


# === TEST ===

if __name__ == "__main__":
    import sys
    import yaml
    
    logging.basicConfig(level=logging.DEBUG, format='%(levelname)s - %(message)s')
    
    # Ladda sökvägar från config
    def _load_test_paths():
        config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'config', 'my_mem_config.yaml')
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            return [
                os.path.expanduser(config['paths']['lake_store']),
                os.path.expanduser(config['paths']['asset_documents']),
            ]
        except Exception as e:
            LOGGER.warning(f"Kunde inte ladda config: {e}")
            return []
    
    if len(sys.argv) < 2:
        print("Användning: python date_service.py <filepath> [filepath2] ...")
        print("\nTestar med exempelfiler...")
        
        test_paths = _load_test_paths()
        
        for test_dir in test_paths:
            if os.path.exists(test_dir):
                print(f"\n=== {test_dir} ===")
                files = [f for f in os.listdir(test_dir) if not f.startswith('.')][:5]
                for f in files:
                    filepath = os.path.join(test_dir, f)
                    if os.path.isfile(filepath):
                        try:
                            date = get_date(filepath)
                            ts = get_timestamp(filepath)
                            print(f"  {f[:50]:50} → {date} ({ts})")
                        except RuntimeError as e:
                            LOGGER.error(f"HARDFAIL för {f}: {e}")
                            print(f"  {f[:50]:50} → HARDFAIL: {e}")
    else:
        for filepath in sys.argv[1:]:
            filepath = os.path.expanduser(filepath)
            try:
                date = get_date(filepath)
                ts = get_timestamp(filepath)
                print(f"{filepath}")
                print(f"  Date: {date}")
                print(f"  Timestamp: {ts}")
            except RuntimeError as e:
                LOGGER.error(f"ERROR för {filepath}: {e}")
                print(f"{filepath}")
                print(f"  ERROR: {e}")
