from typing import TypedDict
import uuid
import time

from langgraph.checkpoint.memory import InMemorySaver
from langgraph_dynamodb_checkpoint import DynamoDBSaver
from langgraph.constants import START
from langgraph.graph import StateGraph
from langgraph.types import interrupt, Command

class State(TypedDict):
    some_text: str

def human_node(state: State):
    value = interrupt( 
        {
            "text_to_revise": state["some_text"] 
        }
    )
    return {
        "some_text": value 
    }


# Build the graph
graph_builder = StateGraph(State)
graph_builder.add_node("human_node", human_node)
graph_builder.add_edge(START, "human_node")

checkpointer = DynamoDBSaver(
    table_name="interrupted_state4"
)

graph = graph_builder.compile(checkpointer=checkpointer)

# Pass a thread ID to the graph to run it.
config = {"configurable": {"thread_id": "20"}}

# Run the graph until the interrupt is hit.
result = graph.invoke({"some_text": "original text"}, config=config) 
print("#####################")
print(checkpointer.get_tuple(config=config))
print("#####################")

print(result['__interrupt__']) 
# > [
# >    Interrupt(
# >       value={'text_to_revise': 'original text'}, 
# >       resumable=True,
# >       ns=['human_node:6ce9e64f-edef-fe5d-f7dc-511fa9526960']
# >    )
# > ] 
#time.sleep(10)
#print('#####' + str(config))
print(graph.invoke(Command(resume="Edited text"), config=config)) 
# > {'some_text': 'Edited text'}