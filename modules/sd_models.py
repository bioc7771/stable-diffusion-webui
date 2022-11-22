import collections
import os.path
import sys
import gc
from collections import namedtuple
import torch
import re
from omegaconf import OmegaConf

from ldm.util import instantiate_from_config

from modules import shared, modelloader, devices, script_callbacks, sd_vae
from modules.paths import models_path
from modules.sd_hijack_inpainting import do_inpainting_hijack, should_hijack_inpainting

model_dir = "Stable-diffusion"
model_path = os.path.abspath(os.path.join(models_path, model_dir))

CheckpointInfo = namedtuple("CheckpointInfo", ['filename', 'title', 'hash', 'model_name', 'config', 'exttype'])
checkpoints_list = {}
checkpoints_loaded = collections.OrderedDict()
checkpoint_types = {'.ckpt':'pickle','.safetensors':'safetensors'}

try:
    # this silences the annoying "Some weights of the model checkpoint were not used when initializing..." message at start.

    from transformers import logging, CLIPModel

    logging.set_verbosity_error()
except Exception:
    pass


def setup_model():
    if not os.path.exists(model_path):
        os.makedirs(model_path)

    list_models()


def checkpoint_tiles(): 
    convert = lambda name: int(name) if name.isdigit() else name.lower() 
    alphanumeric_key = lambda key: [convert(c) for c in re.split('([0-9]+)', key)] 
    return sorted([x.title for x in checkpoints_list.values()], key = alphanumeric_key)


def list_models():
    checkpoints_list.clear()
    model_list = modelloader.load_models(model_path=model_path, command_path=shared.cmd_opts.ckpt_dir, ext_filter=[".ckpt",".safetensors"])

    def modeltitle(path, shorthash):
        abspath = os.path.abspath(path)

        if shared.cmd_opts.ckpt_dir is not None and abspath.startswith(shared.cmd_opts.ckpt_dir):
            name = abspath.replace(shared.cmd_opts.ckpt_dir, '')
        elif abspath.startswith(model_path):
            name = abspath.replace(model_path, '')
        else:
            name = os.path.basename(path)

        if name.startswith("\\") or name.startswith("/"):
            name = name[1:]

        shortname, ext = os.path.splitext(name.replace("/", "_").replace("\\", "_"))

        return f'{name} [{checkpoint_types[ext]}] [{shorthash}]', shortname

    cmd_ckpt = shared.cmd_opts.ckpt
    if os.path.exists(cmd_ckpt):
        h = model_hash(cmd_ckpt)
        title, short_model_name = modeltitle(cmd_ckpt, h)
        checkpoints_list[title] = CheckpointInfo(cmd_ckpt, title, h, short_model_name, shared.cmd_opts.config, '')
        shared.opts.data['sd_model_checkpoint'] = title
    elif cmd_ckpt is not None and cmd_ckpt != shared.default_sd_model_file:
        print(f"Checkpoint in --ckpt argument not found (Possible it was moved to {model_path}: {cmd_ckpt}", file=sys.stderr)
    for filename in model_list:
        h = model_hash(filename)
        title, short_model_name = modeltitle(filename, h)

        basename, ext = os.path.splitext(filename)
        config = basename + ".yaml"
        if not os.path.exists(config):
            config = shared.cmd_opts.config

        checkpoints_list[title] = CheckpointInfo(filename, title, h, short_model_name, config, ext)


def get_closet_checkpoint_match(searchString):
    applicable = sorted([info for info in checkpoints_list.values() if searchString in info.title], key = lambda x:len(x.title))
    if len(applicable) > 0:
        return applicable[0]
    return None


def model_hash(filename):
    try:
        with open(filename, "rb") as file:
            import hashlib
            m = hashlib.sha256()

            file.seek(0x100000)
            m.update(file.read(0x10000))
            return m.hexdigest()[0:8]
    except FileNotFoundError:
        return 'NOFILE'


def select_checkpoint():
    model_checkpoint = shared.opts.sd_model_checkpoint
    checkpoint_info = checkpoints_list.get(model_checkpoint, None)
    if checkpoint_info is not None:
        return checkpoint_info

    if len(checkpoints_list) == 0:
        print(f"No checkpoints found. When searching for checkpoints, looked at:", file=sys.stderr)
        if shared.cmd_opts.ckpt is not None:
            print(f" - file {os.path.abspath(shared.cmd_opts.ckpt)}", file=sys.stderr)
        print(f" - directory {model_path}", file=sys.stderr)
        if shared.cmd_opts.ckpt_dir is not None:
            print(f" - directory {os.path.abspath(shared.cmd_opts.ckpt_dir)}", file=sys.stderr)
        print(f"Can't run without a checkpoint. Find and place a .ckpt file into any of those locations. The program will exit.", file=sys.stderr)
        exit(1)

    checkpoint_info = next(iter(checkpoints_list.values()))
    if model_checkpoint is not None:
        print(f"Checkpoint {model_checkpoint} not found; loading fallback {checkpoint_info.title}", file=sys.stderr)

    return checkpoint_info


chckpoint_dict_replacements = {
    'cond_stage_model.transformer.embeddings.': 'cond_stage_model.transformer.text_model.embeddings.',
    'cond_stage_model.transformer.encoder.': 'cond_stage_model.transformer.text_model.encoder.',
    'cond_stage_model.transformer.final_layer_norm.': 'cond_stage_model.transformer.text_model.final_layer_norm.',
}


def transform_checkpoint_dict_key(k):
    for text, replacement in chckpoint_dict_replacements.items():
        if k.startswith(text):
            k = replacement + k[len(text):]

    return k

def torch_load(model_filename, model_info, map_override=None):
    map_override=shared.weight_load_location if not map_override else map_override
    if(checkpoint_types[model_info.exttype] == 'safetensors'):
        # safely load weights
        # TODO: safetensors supports zero copy fast load to gpu, see issue #684.  
        # GPU only for now, see https://github.com/huggingface/safetensors/issues/95
        try:
            from safetensors.torch import load_file
        except ImportError as e:
            raise ImportError(f"The model is in safetensors format and it is not installed, use `pip install safetensors`: {e}")
        return load_file(model_filename, device='cuda')
    else:
        return torch.load(model_filename, map_location=map_override)

def torch_save(model, output_filename):
    basename, exttype = os.path.splitext(output_filename)
    if(checkpoint_types[exttype] == 'safetensors'):
        # [=====  >] Reticulating brines...
        try:
            from safetensors.torch import save_file
        except ImportError as e:
            raise ImportError(f"Export as safetensors selected, yet it is not installed, use `pip install safetensors`: {e}")
        save_file(model, output_filename, metadata={"format": "pt"})
    else:
        torch.save(model, output_filename)

def get_state_dict_from_checkpoint(pl_sd):
    if "state_dict" in pl_sd:
        pl_sd = pl_sd["state_dict"]

    sd = {}
    for k, v in pl_sd.items():
        new_key = transform_checkpoint_dict_key(k)

        if new_key is not None:
            sd[new_key] = v

    pl_sd.clear()
    pl_sd.update(sd)

    return pl_sd


def load_model_weights(model, checkpoint_info, vae_file="auto"):
    checkpoint_file = checkpoint_info.filename
    sd_model_hash = checkpoint_info.hash

    cache_enabled = shared.opts.sd_checkpoint_cache > 0

    if cache_enabled and checkpoint_info in checkpoints_loaded:
        # use checkpoint cache
        print(f"Loading weights [{sd_model_hash}] from cache")
        model.load_state_dict(checkpoints_loaded[checkpoint_info])
    else:
        # load from file
        print(f"Loading weights [{sd_model_hash}] from {checkpoint_file}")

        pl_sd = torch_load(checkpoint_file, checkpoint_info)

        if "global_step" in pl_sd:
            print(f"Global Step: {pl_sd['global_step']}")

        sd = get_state_dict_from_checkpoint(pl_sd)
        del pl_sd
        model.load_state_dict(sd, strict=False)
        del sd
        
        if cache_enabled:
            # cache newly loaded model
            checkpoints_loaded[checkpoint_info] = model.state_dict().copy()

        if shared.cmd_opts.opt_channelslast:
            model.to(memory_format=torch.channels_last)

        if not shared.cmd_opts.no_half:
            vae = model.first_stage_model

            # with --no-half-vae, remove VAE from model when doing half() to prevent its weights from being converted to float16
            if shared.cmd_opts.no_half_vae:
                model.first_stage_model = None

            model.half()
            model.first_stage_model = vae

        devices.dtype = torch.float32 if shared.cmd_opts.no_half else torch.float16
        devices.dtype_vae = torch.float32 if shared.cmd_opts.no_half or shared.cmd_opts.no_half_vae else torch.float16

        model.first_stage_model.to(devices.dtype_vae)

    # clean up cache if limit is reached
    if cache_enabled:
        while len(checkpoints_loaded) > shared.opts.sd_checkpoint_cache + 1: # we need to count the current model
            checkpoints_loaded.popitem(last=False)  # LRU

    model.sd_model_hash = sd_model_hash
    model.sd_model_checkpoint = checkpoint_file
    model.sd_checkpoint_info = checkpoint_info

    vae_file = sd_vae.resolve_vae(checkpoint_file, vae_file=vae_file)
    sd_vae.load_vae(model, vae_file)


def load_model(checkpoint_info=None):
    from modules import lowvram, sd_hijack
    checkpoint_info = checkpoint_info or select_checkpoint()

    if checkpoint_info.config != shared.cmd_opts.config:
        print(f"Loading config from: {checkpoint_info.config}")

    if shared.sd_model:
        sd_hijack.model_hijack.undo_hijack(shared.sd_model)
        shared.sd_model = None
        gc.collect()
        devices.torch_gc()

    sd_config = OmegaConf.load(checkpoint_info.config)
    
    if should_hijack_inpainting(checkpoint_info):
        # Hardcoded config for now...
        sd_config.model.target = "ldm.models.diffusion.ddpm.LatentInpaintDiffusion"
        sd_config.model.params.use_ema = False
        sd_config.model.params.conditioning_key = "hybrid"
        sd_config.model.params.unet_config.params.in_channels = 9

        # Create a "fake" config with a different name so that we know to unload it when switching models.
        checkpoint_info = checkpoint_info._replace(config=checkpoint_info.config.replace(".yaml", "-inpainting.yaml"))

    do_inpainting_hijack()

    sd_model = instantiate_from_config(sd_config.model)
    load_model_weights(sd_model, checkpoint_info)

    if shared.cmd_opts.lowvram or shared.cmd_opts.medvram:
        lowvram.setup_for_low_vram(sd_model, shared.cmd_opts.medvram)
    else:
        sd_model.to(shared.device)

    sd_hijack.model_hijack.hijack(sd_model)

    sd_model.eval()
    shared.sd_model = sd_model

    script_callbacks.model_loaded_callback(sd_model)

    print(f"Model loaded.")
    return sd_model


def reload_model_weights(sd_model=None, info=None):
    from modules import lowvram, devices, sd_hijack
    checkpoint_info = info or select_checkpoint()
 
    if not sd_model:
        sd_model = shared.sd_model

    if sd_model.sd_model_checkpoint == checkpoint_info.filename:
        return

    if sd_model.sd_checkpoint_info.config != checkpoint_info.config or should_hijack_inpainting(checkpoint_info) != should_hijack_inpainting(sd_model.sd_checkpoint_info):
        del sd_model
        checkpoints_loaded.clear()
        load_model(checkpoint_info)
        return shared.sd_model

    if shared.cmd_opts.lowvram or shared.cmd_opts.medvram:
        lowvram.send_everything_to_cpu()
    else:
        sd_model.to(devices.cpu)

    sd_hijack.model_hijack.undo_hijack(sd_model)

    load_model_weights(sd_model, checkpoint_info)

    sd_hijack.model_hijack.hijack(sd_model)
    script_callbacks.model_loaded_callback(sd_model)

    if not shared.cmd_opts.lowvram and not shared.cmd_opts.medvram:
        sd_model.to(devices.device)

    print(f"Weights loaded.")
    return sd_model
