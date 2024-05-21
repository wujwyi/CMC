import torch
import json
from typing import Any, Dict, Generator, List, Optional, Tuple
from threading import Thread
from transformers import GenerationConfig, TextIteratorStreamer
from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
)

from llm_base.config_parser import get_infer_args
from llm_base.model_trainer import get_logits_processor
from data_process.data_utils import get_template_and_fix_tokenizer


class ChatModel:
    def __init__(self, args: Optional[Dict[str, Any]] = None) -> None:
        (
            model_args,
            self.data_args,
            finetuning_args,
            self.generating_args,
        ) = get_infer_args(args)
        self.model, self.tokenizer = self.get_model(model_args.model_name_or_path)
        self.tokenizer.padding_side = "left"
        self.model = self.model.cuda()
        self.template = get_template_and_fix_tokenizer(
            self.data_args.template, self.tokenizer
        )
        self.system_prompt = None

    def get_model(self, model_path):
        anchor_path = model_path
        config_kwargs = {
            "trust_remote_code": True,
            "cache_dir": None,
            "revision": "main",
            "use_auth_token": None,
        }
        tokenizer = AutoTokenizer.from_pretrained(
            anchor_path,
            use_fast=False,
            **config_kwargs
        )
        config = AutoConfig.from_pretrained(anchor_path, **config_kwargs)

        # return tokenizer
        print("loading {}...".format(anchor_path))
        model = AutoModelForCausalLM.from_pretrained(
            anchor_path,
            config=config,
            torch_dtype=torch.bfloat16,
            **config_kwargs
        )
    
        return model,tokenizer

    def process_args(
        self,
        query: str,
        history: Optional[List[Tuple[str, str]]] = None,
        system: Optional[str] = None,
        **input_kwargs
    ) -> Tuple[Dict[str, Any], int]:
        system = system or self.system_prompt

        prompt, _ = self.template.encode_oneturn(
            tokenizer=self.tokenizer,
            query=query,
            resp="",
            history=history,
            system=system,
        )
        input_ids = torch.tensor([prompt], device=self.model.device)
        prompt_length = len(input_ids[0])

        do_sample = input_kwargs.pop("do_sample", None)
        temperature = input_kwargs.pop("temperature", None)
        top_p = input_kwargs.pop("top_p", None)
        top_k = input_kwargs.pop("top_k", None)
        repetition_penalty = input_kwargs.pop("repetition_penalty", None)
        max_length = input_kwargs.pop("max_length", None)
        max_new_tokens = input_kwargs.pop("max_new_tokens", None)

        generating_args = self.generating_args.to_dict()
        generating_args.update(
            dict(
                do_sample=do_sample
                if do_sample is not None
                else generating_args["do_sample"],
                temperature=temperature or generating_args["temperature"],
                top_p=top_p or generating_args["top_p"],
                top_k=top_k or generating_args["top_k"],
                repetition_penalty=repetition_penalty
                or generating_args["repetition_penalty"],
                eos_token_id=[self.tokenizer.eos_token_id]
                + self.tokenizer.additional_special_tokens_ids,
                pad_token_id=self.tokenizer.pad_token_id,
            )
        )

        if max_length:
            generating_args.pop("max_new_tokens", None)
            generating_args["max_length"] = max_length

        if max_new_tokens:
            generating_args.pop("max_length", None)
            generating_args["max_new_tokens"] = max_new_tokens

        gen_kwargs = dict(
            inputs=input_ids,
            generation_config=GenerationConfig(**generating_args),
            logits_processor=get_logits_processor(),
        )

        return gen_kwargs, prompt_length

    @torch.inference_mode()
    def chat(
        self,
        query: str,
        history: Optional[List[Tuple[str, str]]] = None,
        system: Optional[str] = None,
        **input_kwargs
    ) -> Tuple[str, Tuple[int, int]]:
        gen_kwargs, prompt_length = self.process_args(
            query, history, system, **input_kwargs
        )
        generation_output = self.model.generate(**gen_kwargs)
        outputs = generation_output.tolist()[0][prompt_length:]
        response = self.tokenizer.decode(outputs, skip_special_tokens=True)
        response_length = len(outputs)
        return response

    @torch.inference_mode()
    def stream_chat(
        self,
        query: str,
        history: Optional[List[Tuple[str, str]]] = None,
        system: Optional[str] = None,
        **input_kwargs
    ) -> Generator[str, None, None]:
        gen_kwargs, _ = self.process_args(query, history, system, **input_kwargs)
        streamer = TextIteratorStreamer(
            self.tokenizer, timeout=60.0, skip_prompt=True, skip_special_tokens=True
        )
        gen_kwargs["streamer"] = streamer

        thread = Thread(target=self.model.generate, kwargs=gen_kwargs)
        thread.start()

        yield from streamer
