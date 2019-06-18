import torch  # must be imported before anything from signatory

from .backend import (signature,
                      signature_channels)
from .modules import (Signature,
                      Augment)


__version__ = "0.1.2"

del torch
