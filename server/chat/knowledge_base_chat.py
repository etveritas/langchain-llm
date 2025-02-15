from fastapi import Body, Request
from fastapi.responses import StreamingResponse
from configs.model_config import (llm_model_dict, LLM_MODEL, PROMPT_TEMPLATE,
                                  VECTOR_SEARCH_TOP_K, SCORE_THRESHOLD)
from configs.model_config import (QTPL_PROMPT, KTPL_PROMPT)
from server.chat.utils import wrap_done
from server.utils import BaseResponse
from langchain.chat_models import ChatOpenAI
from models.chatglm import ChatChatGLM
from langchain.llms import ChatGLM, OpenAI
from langchain import LLMChain
from langchain.callbacks import AsyncIteratorCallbackHandler
from typing import AsyncIterable, List, Optional
import asyncio
from langchain.prompts.chat import ChatPromptTemplate
from server.chat.utils import History
from server.knowledge_base.kb_service.base import KBService, KBServiceFactory
import json
import os
import uuid
import numpy as np
from collections import defaultdict
from urllib.parse import urlencode
from server.knowledge_base.kb_doc_api import search_docs


def sigmoid(x):
    return 1/(1+np.exp(-x))

def knowledge_base_chat(query: str = Body(..., description="用户输入", examples=["你好"]),
                        knowledge_base_name: str = Body(..., description="知识库名称", examples=["samples"]),
                        top_k: int = Body(VECTOR_SEARCH_TOP_K, description="匹配向量数"),
                        score_threshold: float = Body(SCORE_THRESHOLD, description="知识库匹配相关度阈值，取值范围在0-1之间，SCORE越小，相关度越高，取到1相当于不筛选，建议设置在0.5左右", ge=0, le=1100),
                        history: List[History] = Body([],
                                                      description="历史对话",
                                                      examples=[[
                                                          {"role": "user",
                                                           "content": "我们来玩成语接龙，我先来，生龙活虎"},
                                                          {"role": "assistant",
                                                           "content": "虎头虎脑"}]]
                                                      ),
                        stream: bool = Body(False, description="流式输出"),
                        model_name: str = Body(LLM_MODEL, description="LLM 模型名称。"),
                        local_doc_url: bool = Body(False, description="知识文件返回本地路径(true)或URL(false)"),
                        request: Request = None,
                        ):
    kb = KBServiceFactory.get_service_by_name(knowledge_base_name)
    if kb is None:
        return BaseResponse(code=404, msg=f"未找到知识库 {knowledge_base_name}")

    history = [History.from_data(h) for h in history]

    async def knowledge_base_chat_iterator(query: str,
                                           kb: KBService,
                                           top_k: int,
                                           history: Optional[List[History]],
                                           model_name: str = LLM_MODEL,
                                           ) -> AsyncIterable[str]:
        callback = AsyncIteratorCallbackHandler()
        if "gpt" in model_name:
            model = ChatOpenAI(
                temperature=0.1,
                streaming=True,
                verbose=True,
                callbacks=[callback],
                openai_api_key=llm_model_dict[model_name]["api_key"],
                openai_api_base=llm_model_dict[model_name]["api_base_url"],
                model_name=model_name,
            openai_proxy=llm_model_dict[model_name].get("openai_proxy")
            )
        elif "glm" in model_name:
            model = ChatChatGLM(
                temperature=0.1,
                streaming=True,
                verbose=True,
                callbacks=[callback],
                chatglm_api_key=llm_model_dict[model_name]["api_key"],
                chatglm_api_base=llm_model_dict[model_name]["api_base_url"],
                model_name=model_name
            )
        docs = search_docs(query, knowledge_base_name, top_k, score_threshold)
        context = "\n".join([doc.page_content for doc in docs])

        # input_msg = History(role="user", content=PROMPT_TEMPLATE).to_msg_template(False)
        chat_prompt = ChatPromptTemplate.from_messages(
            [i.to_msg_tuple() for i in history]
            + [("human", KTPL_PROMPT)]
            + [("human", QTPL_PROMPT)]
        )

        chain = LLMChain(prompt=chat_prompt, llm=model)

        # combine prompt
        prompt_comb = chain.prep_prompts([{"context": context, "question": query}])

        # Begin a task that runs in the background.
        task = asyncio.create_task(wrap_done(
            chain.acall({"context": context, "question": query}),
            callback.done),
        )

        source_documents = []
        reference_list = defaultdict(list)
        for inum, doc in enumerate(docs):
            filename = os.path.split(doc.metadata["source"])[-1]
            if local_doc_url:
                url = "file://" + doc.metadata["source"]
            else:
                parameters = urlencode({"knowledge_base_name": knowledge_base_name, "file_name":filename})
                url = f"{request.base_url}knowledge_base/download_doc?" + parameters
            text = f"""出处 [{inum + 1}] [{filename}]({url}) \n\n{doc.page_content}\n\n 相似度：{1100-doc.score}\n\n"""
            
            reference_list[filename].append([doc.page_content, str(int(1100-doc.score))])
            source_documents.append(text)
        reference_list = dict(reference_list)
        unq_id = uuid.uuid1()
        if stream:
            async for token in callback.aiter():
                # Use server-sent-events to stream the response
                yield json.dumps({"uuid": str(unq_id),
                                  "answer": token,
                                  "docs": source_documents,
                                  "reference": reference_list,
                                  "prompt": prompt_comb[0][0].to_string()},
                                 ensure_ascii=False)
        else:
            answer = ""
            async for token in callback.aiter():
                answer += token
            yield json.dumps({"uuid": str(unq_id),
                              "answer": answer,
                              "docs": source_documents,
                              "reference": reference_list,
                              "prompt": prompt_comb[0][0].to_string()},
                             ensure_ascii=False)

        await task

    def syn_knowledge_base_chat_iterator(query: str,
                                           kb: KBService,
                                           top_k: int,
                                           history: Optional[List[History]],
                                           model_name: str = LLM_MODEL,
                                           ) -> AsyncIterable[str]:
        if "gpt" in model_name:
            model = ChatOpenAI(
                temperature=0.1,
                streaming=False,
                verbose=True,
                openai_api_key=llm_model_dict[model_name]["api_key"],
                openai_api_base=llm_model_dict[model_name]["api_base_url"],
                model_name=model_name,
            openai_proxy=llm_model_dict[model_name].get("openai_proxy")
            )
        elif "glm" in model_name:
            model = ChatChatGLM(
                temperature=0.1,
                streaming=False,
                verbose=True,
                chatglm_api_key=llm_model_dict[model_name]["api_key"],
                chatglm_api_base=llm_model_dict[model_name]["api_base_url"],
                model_name=model_name
            )
        docs = search_docs(query, knowledge_base_name, top_k, score_threshold)
        context = "\n".join([doc.page_content for doc in docs])

        # input_msg = History(role="user", content=PROMPT_TEMPLATE).to_msg_template(False)
        chat_prompt = ChatPromptTemplate.from_messages(
            [i.to_msg_tuple() for i in history]
            + [("human", KTPL_PROMPT)]
            + [("human", QTPL_PROMPT)]
        )

        chain = LLMChain(prompt=chat_prompt, llm=model)

        # combine prompt
        prompt_comb = chain.prep_prompts([{"context": context, "question": query}])

        # Begin a task that runs in the background.
        res_iter = chain.run({"context": context, "question": query})

        source_documents = []
        reference_list = defaultdict(list)
        for inum, doc in enumerate(docs):
            filename = os.path.split(doc.metadata["source"])[-1]
            if local_doc_url:
                url = "file://" + doc.metadata["source"]
            else:
                parameters = urlencode({"knowledge_base_name": knowledge_base_name, "file_name":filename})
                url = f"{request.base_url}knowledge_base/download_doc?" + parameters
            text = f"""出处 [{inum + 1}] [{filename}]({url}) \n\n{doc.page_content}\n\n 相似度：{1100-doc.score}\n\n"""
            
            reference_list[filename].append([doc.page_content, str(int(1100-doc.score))])
            source_documents.append(text)
        reference_list = dict(reference_list)
        unq_id = uuid.uuid1()
        if stream:
            for token in res_iter:
                # Use server-sent-events to stream the response
                yield json.dumps({"uuid": str(unq_id),
                                  "answer": token,
                                  "docs": source_documents,
                                  "reference": reference_list,
                                  "prompt": prompt_comb[0][0].to_string()},
                                 ensure_ascii=False)
        else:
            answer = ""
            for token in res_iter:
                answer += token
            yield json.dumps({"uuid": str(unq_id),
                              "answer": answer,
                              "docs": source_documents,
                              "reference": reference_list,
                              "prompt": prompt_comb[0][0].to_string()},
                             ensure_ascii=False)

    return StreamingResponse(syn_knowledge_base_chat_iterator(query, kb, top_k, history, model_name),
                             media_type="text/event-stream")
