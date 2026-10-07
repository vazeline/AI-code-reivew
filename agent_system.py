from typing import TypedDict
from langgraph.graph import StateGraph, END

# 1. Вот он — наш "Блокнот" (State). Это просто структура данных.
class AgentState(TypedDict):
    task: str          # Описание задачи (бага)
    code_fix: str      # Сюда программист запишет исправление
    attempts: int      # Счетчик попыток

# 2. Пишем первого Агента (это обычная функция на Python)
def programmer_agent(state: AgentState):
    print("--- Агент-Программист включился в работу ---")
    print(f"Читаю задачу из блокнота: {state['task']}")
    
    # Здесь в будущем будет запрос к ChatGPT/LLM, а пока пишем заглушку:
    generated_code = "def fix_function(): return 'Fixed Code!'"
    
    # Агент делает запись в наш блокнот (возвращает обновленные данные)
    return {
        "code_fix": generated_code, 
        "attempts": state["attempts"] + 1
    }

# 3. Собираем нашу систему (Граф)
workflow = StateGraph(AgentState)

# Добавляем нашего программиста на карту воркфлоу
workflow.add_node("programmer", programmer_agent)

# Задаем логику: старт начинается с программиста, а после него — конец (END)
workflow.set_entry_point("programmer")
workflow.add_edge("programmer", END)

# Компилируем (запускаем сборку)
app = workflow.compile()

# 4. Проверяем, как это работает!
# Передаем начальное состояние "блокнота"
initial_input = {"task": "Починить функцию деления на ноль", "code_fix": "", "attempts": 0}
final_output = app.invoke(initial_input)

print("\n--- Результат работы системы ---")
print(final_output)
