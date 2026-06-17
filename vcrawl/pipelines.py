from dataclasses import dataclass


@dataclass
class PipelineContext:
    page_url: str
    title: str = ""


class VideoPipeline:
    def process(self, candidate, context):
        return candidate


class DedupeVideoPipeline(VideoPipeline):
    def __init__(self):
        self._seen = set()

    def process(self, candidate, context):
        key = (candidate.page_url, candidate.media_url)
        if key in self._seen:
            return None
        self._seen.add(key)
        return candidate


class PipelineRunner:
    def __init__(self, pipelines=None):
        self.pipelines = list(pipelines or [DedupeVideoPipeline()])

    def process_videos(self, videos, context):
        output = []
        for candidate in videos:
            current = candidate
            for pipeline in self.pipelines:
                if current is None:
                    break
                current = pipeline.process(current, context)
            if current is not None:
                output.append(current)
        return output
