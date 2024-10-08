import os, matplotlib
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
import shutil
from transformers import AutoProcessor, VisionEncoderDecoderModel
import torch
from huggingface_hub import hf_hub_download
from typing import Optional, List
import io
import fitz
from pathlib import Path
from transformers import StoppingCriteria, StoppingCriteriaList
from collections import defaultdict
from PIL import Image

processor = AutoProcessor.from_pretrained("facebook/nougat-base")
model = VisionEncoderDecoderModel.from_pretrained("facebook/nougat-base")
device = "cuda" if torch.cuda.is_available() else "cpu"
model.to(device)

class RunningVarTorch:
    def __init__(self, L=15, norm=False):
        self.values = None
        self.L = L
        self.norm = norm

    def push(self, x: torch.Tensor):
        assert x.dim() == 1
        if self.values is None:
            self.values = x[:, None]
        elif self.values.shape[1] < self.L:

            self.values = torch.cat((self.values, x[:, None]), 1)
        else:
            self.values = torch.cat((self.values[:, 1:], x[:, None]), 1)

    def variance(self):
        if self.values is None:
            return
        if self.norm:
            return torch.var(self.values, 1) / self.values.shape[1]
        else:
            return torch.var(self.values, 1)


class StoppingCriteriaScores(StoppingCriteria):
    def __init__(self, threshold: float = 0.015, window_size: int = 200):
        super().__init__()
        self.threshold = threshold
        self.vars = RunningVarTorch(norm=True)
        self.varvars = RunningVarTorch(L=window_size)
        self.stop_inds = defaultdict(int)
        self.stopped = defaultdict(bool)
        self.size = 0
        self.window_size = window_size

    @torch.no_grad()
    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor):
        last_scores = scores[-1]
        self.vars.push(last_scores.max(1)[0].float().cpu())
        self.varvars.push(self.vars.variance())
        self.size += 1
        if self.size < self.window_size:
            return False

        varvar = self.varvars.variance()
        for b in range(len(last_scores)):
            if varvar[b] < self.threshold:
                if self.stop_inds[b] > 0 and not self.stopped[b]:
                    self.stopped[b] = self.stop_inds[b] >= self.size
                else:
                    self.stop_inds[b] = int(
                        min(max(self.size, 1) * 1.15 + 150 + self.window_size, 4095)
                    )
            else:
                self.stop_inds[b] = 0
                self.stopped[b] = False
        return all(self.stopped.values()) and len(self.stopped) > 0

def rasterize_paper(
    pdf: Path,
    outpath: Optional[Path] = None,
    dpi: int = 96,
    return_pil=False,
    pages=None,
) -> Optional[List[io.BytesIO]]:
    """
    Rasterize a PDF file to PNG images.

    Args:
        pdf (Path): The path to the PDF file.
        outpath (Optional[Path], optional): The output directory. If None, the PIL images will be returned instead. Defaults to None.
        dpi (int, optional): The output DPI. Defaults to 96.
        return_pil (bool, optional): Whether to return the PIL images instead of writing them to disk. Defaults to False.
        pages (Optional[List[int]], optional): The pages to rasterize. If None, all pages will be rasterized. Defaults to None.

    Returns:
        Optional[List[io.BytesIO]]: The PIL images if `return_pil` is True, otherwise None.
    """

    pillow_images = []
    if outpath is None:
        return_pil = True
    try:
        if isinstance(pdf, (str, Path)):
            pdf = fitz.open(pdf)
        if pages is None:
            pages = range(len(pdf))
        for i in pages:
            page_bytes: bytes = pdf[i].get_pixmap(dpi=dpi).pil_tobytes(format="PNG")
            if return_pil:
                pillow_images.append(io.BytesIO(page_bytes))
            else:
                with (outpath / ("%02d.png" % (i + 1))).open("wb") as f:
                    f.write(page_bytes)
    except Exception:
        pass
    if return_pil:
        return pillow_images

write_directory = '/home/louis/data/processed_data2/text_data/'
try:
    os.mkdir(write_directory)
except(FileExistsError):
    pass

# walk through all subdirectories of root_path
root_path = "/home/louis/data/raw_data/pdf/10.1103"
for pdf_path, directories, files in os.walk(root_path):
    for file in files:
        pdffile_name = os.fsdecode(file)
        if pdffile_name.endswith('.pdf'):
            print(pdffile_name)
            current_dir = os.path.join(write_directory, os.path.splitext(pdffile_name)[0])

            #check if pdf has already been processed
            try:
                os.mkdir(current_dir)
            except(FileExistsError):
                print("this path already exists, skipping...")
                continue

            # run extraction
            try:    
                this_pdf_text = []
                images = rasterize_paper(pdf=os.path.join(pdf_path, pdffile_name), return_pil=True)
                for image_file in images:
                    image = Image.open(image_file)
                    pixel_values = processor(images=image, return_tensors="pt").pixel_values

                    # autoregressively generate tokens, with custom stopping criteria (as defined by the Nougat authors)
                    outputs = model.generate(pixel_values.to(device),
                          min_length=1,
                          max_length=3584,
                          bad_words_ids=[[processor.tokenizer.unk_token_id]],
                          return_dict_in_generate=True,
                          output_scores=True,
                          stopping_criteria=StoppingCriteriaList([StoppingCriteriaScores()]),
                                             )
                    generated = processor.batch_decode(outputs[0], skip_special_tokens=True)[0]
                    generated = processor.post_process_generation(generated, fix_markdown=False)
                    this_pdf_text.append(generated)

                output_text = " ".join(this_pdf_text)
                    
                # output text
                with open(os.path.join(current_dir, 'text.txt'), 'w') as f:
                    f.write(output_text)

                # copy pdf to target directory
                shutil.copy(os.path.join(pdf_path, pdffile_name), os.path.join(current_dir, pdffile_name))

            # If an exception occurs, delete the working output dir

            # quit if keyboard interrupt
            except KeyboardInterrupt:
                shutil.rmtree(current_dir)
                with open('failed.txt', 'a') as f:
                    f.write(pdffile_name)
                sys.exit() # lol

            # keep going if other kind of error, write filename to failed.txt
            except:
                print("{} failed.".format(pdffile_name))
                shutil.rmtree(current_dir)
                with open('failed.txt', 'a') as f:
                    f.write(pdffile_name)

        else:
            continue
