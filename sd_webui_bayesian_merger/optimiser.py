import os
from abc import abstractmethod
from pathlib import Path
from typing import Dict, List
from dataclasses import dataclass

import json

from tqdm import tqdm
from omegaconf import DictConfig
from bayes_opt.logger import JSONLogger
from hydra.core.hydra_config import HydraConfig

from sd_webui_bayesian_merger.generator import Generator
from sd_webui_bayesian_merger.prompter import Prompter
from sd_webui_bayesian_merger.merger import Merger, NUM_TOTAL_BLOCKS
from sd_webui_bayesian_merger.scorer import AestheticScorer
from sd_webui_bayesian_merger.artist import draw_unet, convergence_plot

PathT = os.PathLike


@dataclass
class Optimiser:
    cfg: DictConfig
    best_rolling_score: float = 0.0

    def __post_init__(self) -> None:
        self.generator = Generator(self.cfg.url, self.cfg.batch_size)
        self.merger = Merger(self.cfg)
        self.start_logging()
        self.scorer = AestheticScorer(self.cfg)
        self.prompter = Prompter(self.cfg)
        self.iteration = 0
        self._clean = True
        self.has_beta = self.cfg.merge_mode in ["sum_twice", "triple_sum"]

    def cleanup(self) -> None:
        if self._clean:
            # clean up and remove the last merge
            self.merger.remove_previous_ckpt(self.iteration)
        else:
            self._clean = True

    def start_logging(self) -> None:
        run_name = "-".join(self.merger.output_file.stem.split("-")[:-1])
        self.log_name = f"{run_name}-{self.cfg.optimiser}"
        self.logger = JSONLogger(
            path=str(
                Path(
                    HydraConfig.get().runtime.output_dir,
                    f"{self.log_name}.json",
                )
            )
        )

    def sd_target_function(self, **params):
        self.iteration += 1

        if self.iteration == 1:
            print("\n" + "-" * 10 + " warmup " + "-" * 10 + ">")
        elif self.iteration == self.cfg.init_points + 1:
            print("\n" + "-" * 10 + " optimisation " + "-" * 10 + ">")

        it_type = "warmup" if self.iteration <= self.cfg.init_points else "optimisation"
        print(f"\n{it_type} - Iteration: {self.iteration}")

        weights_alpha = [params[f"block_{i}"] for i in range(NUM_TOTAL_BLOCKS)]
        base_alpha = params["base_alpha"]
        if self.has_beta:
            base_beta = params["base_beta"]
            weights_beta = [params[f"block_{i}_beta"] for i in range(NUM_TOTAL_BLOCKS)]
        else:
            base_beta = None
            weights_beta = None

        self.merger.create_model_out_name(self.iteration)
        self.merger.merge(weights_alpha, weights_beta, base_alpha, base_beta)
        self.cleanup()

        self.generator.switch_model(self.merger.model_out_name)

        # generate images
        images = []
        payloads, paths = self.prompter.render_payloads()
        gen_paths = []
        for i, payload in tqdm(
            enumerate(payloads),
            desc="Batches generation",
        ):
            images.extend(self.generator.batch_generate(payload))
            gen_paths.extend([paths[i]] * self.cfg.batch_size * payload["batch_size"])

        # score images
        print("\nScoring")
        scores = self.scorer.batch_score(
            images,
            gen_paths,
            self.iteration,
        )

        # spit out a single value for optimisation
        avg_score = self.scorer.average_score(scores)
        print(f"{'-'*10}\nRun score: {avg_score}")

        print(f"\nrun base_alpha: {base_alpha}")
        print("run weights:")
        weights_str = ",".join(list(map(str, weights_alpha)))
        print(weights_str)

        if self.has_beta:
            print(f"\nrun base_beta: {base_beta}")
            print("run weights_beta:")
            weights_beta_str = ",".join(list(map(str, weights_beta)))
            print(weights_beta_str)
        else:
            weights_beta_str = ""

        if avg_score > self.best_rolling_score:
            self.best_rolling_score = avg_score
            print("\n NEW BEST!")
            save_best_log(base_alpha, weights_str, base_beta, weights_beta_str)
            print("Saving best model merge")
            self.merger.keep_best_ckpt()
            self._clean = False

        return avg_score

    @abstractmethod
    def optimise(self) -> None:
        raise NotImplementedError("Not implemented")

    @abstractmethod
    def postprocess(self) -> None:
        raise NotImplementedError("Not implemented")

    def plot_and_save(
        self,
        scores: List[float],
        best_base_alpha: float,
        best_weights_alpha: List[float],
        best_base_beta: float,
        best_weights_beta: List[float],
        minimise: bool,
    ) -> None:
        img_path = Path(
            HydraConfig.get().runtime.output_dir,
            f"{self.log_name}.png",
        )
        convergence_plot(scores, figname=img_path, minimise=minimise)

        unet_path = Path(
            HydraConfig.get().runtime.output_dir,
            f"{self.log_name}-unet.png",
        )
        print("\n" + "-" * 10 + "> Done!")
        print("\nBest run:")
        print("best base_alpha:")
        print(best_base_alpha)
        print("\nbest weights alpha:")
        best_weights_str = ",".join(list(map(str, best_weights_alpha)))
        print(best_weights_str)

        if self.has_beta:
            print("\nbest base_beta:")
            print(best_base_beta)
            print("\nbest weights beta:")
            best_weights_str_beta = ",".join(list(map(str, best_weights_beta)))
            print(best_weights_str_beta)
        else:
            best_weights_str_beta = ""

        save_best_log(
            best_base_alpha,
            best_weights_str,
            best_base_beta,
            best_weights_str_beta,
        )
        draw_unet(
            best_base_alpha,
            best_weights_alpha,
            model_a=Path(self.cfg.model_a).stem,
            model_b=Path(self.cfg.model_b).stem,
            figname=unet_path,
        )
        if self.has_beta:
            unet_path_beta = Path(
                HydraConfig.get().runtime.output_dir,
                f"{self.log_name}-unet_beta.png",
            )
            draw_unet(
                best_base_beta,
                best_weights_beta,
                model_a=Path(self.cfg.model_a).stem,
                model_b=Path(self.cfg.model_b).stem,
                figname=unet_path_beta,
            )

        if self.cfg.save_best:
            print(f"Saving best merge: {self.merger.best_output_file}")
            self.merger.merge(
                best_weights_alpha,
                best_weights_beta,
                best_base_alpha,
                best_base_beta,
                best=True,
            )


def save_best_log(alpha, weights, beta, weights_beta):
    print("Saving best.log")
    with open(
        Path(HydraConfig.get().runtime.output_dir, "best.log"),
        "w",
        encoding="utf-8",
    ) as f:
        f.write(f"{alpha}\n\n{weights}")
        if beta:
            f.write(f"\n{beta}\n\n{weights_beta}")


def load_log(log: PathT) -> List[Dict]:
    iterations = []
    with open(log, "r") as j:
        while True:
            try:
                iteration = next(j)
            except StopIteration:
                break

            iterations.append(json.loads(iteration))

    return iterations
