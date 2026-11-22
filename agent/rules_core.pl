:- encoding(utf8).
% =====================================================================
% 规则引擎核心（不含任何玩法）。玩法规则在 content/<pack>/rules/*.pl。
%
% 事实来自 Neo4j，每次查询前由引擎现读（agent/rulebook.py）：
%   g_node(Label, Id).              节点（Id = name 属性）
%   g_prop(Id, Key, Value).         节点属性（字符串 → 原子，布尔 → true/false）
%   g_rel(Type, A, B, Props).       有向边；Props = [Key-Value, ...]
%   g_now(T).                       模拟时钟
%   g_tick(Prev, Now).              时间钩子：上一次和这一次（只在问 tick/1 时给）
%   sees(W, P)                      W 看得见 P：规则问到时才回头问引擎的感知（P 要已知）；
%                                   给了 g_sees(W, P) 事实就只用这些（测试 / 调用方指定）
%   busy_since(P, T).               问 react/2 时：P 从 T 起正在做一件事
% 读哪些标签 / 关系由规则声明：reads(label('Person')). reads(rel('FEELS')).
%
% 规则应通过下面这些谓词读图——它们会叠加「假设」（推演时）：
%   node(L, Id)  prop(Id, K, V)  rel(T, A, B)  rel(T, A, B, Props)  rel_prop(T, A, B, K, V)
%   now(T)  assumed(Effect)  assumed_op(Op, Effect)
%
% 内容包提供（都可以分散在多个包里）：
%   reads/1                            要读的标签 / 关系
%   after(Event, Effect)               事件发生后的效果；Effect 是和动作 JSON 一样的 op（SWI dict）
%   tick(Effect)                       时间过去后的效果（回落、回血……）；用 every(Period) 限定多久一次
%   tick_period(Period)                tick/1 用到的周期：引擎只在跨过某个周期时才问（不声明就每拍都问）
%   tick_phase(Order, Phase) + tick(Phase, Effect)
%                                      分步的时间效果：先 tick/1，再按 Order 逐步问，每步之前重读世界
%                                      （后一步要看到前一步的结果时用，如先结盟、再算联盟成员）
%   forecast(Event, Text)              做之前提示会怎样
%   react(Me, Choice)                  NPC 要不要丢下手上的事马上行动（打、逃……）；Choice 是 dict：
%                                      goal / target / why / text / source / plan（[[能力名, [参数...]], ...]）
%   label/2  explain/4                 原因的标签 / 一句话解释（引擎原样转给提示词与界面）
%   const(Name, Value)                 引擎在别处也要用的常量
% 事件 = 能力名（没有就用动作名）+ 动作参数（按声明顺序），如 take('Ash', 'Berry1', 'Grove')。
% =====================================================================

:- use_module(library(http/json)).
:- use_module(library(apply)).
:- use_module(library(lists)).

:- dynamic g_node/2, g_prop/3, g_rel/4, g_now/1, g_tick/2, g_sees/2, busy_since/2, hyp/1.
:- multifile reads/1, after/2, tick/1, tick/2, tick_phase/2, tick_period/1, forecast/2, react/2, label/2, explain/4, const/2.
:- dynamic reads/1, after/2, tick/1, tick/2, tick_phase/2, tick_period/1, forecast/2, react/2, label/2, explain/4, const/2.
:- discontiguous reads/1, after/2, tick/1, tick/2, tick_phase/2, tick_period/1, forecast/2, react/2, label/2, explain/4, const/2.

load_facts(Facts) :-
    retractall(g_node(_, _)), retractall(g_prop(_, _, _)), retractall(g_rel(_, _, _, _)),
    retractall(g_now(_)), retractall(g_tick(_, _)), retractall(g_sees(_, _)), retractall(busy_since(_, _)),
    retractall(hyp(_)),
    forall(member(F, Facts), assertz(F)).

% ---------------------------------------------------------------------
% 读图（叠加假设）。假设 = 一组 op（SWI dict）：link / set_prop / add_prop 由这里叠加，
% 包自己约定的 op（如 feel）由包的规则通过 assumed_op/2 读。
% ---------------------------------------------------------------------

node(L, Id) :- g_node(L, Id).

prop(Id, K, V) :-
    base_prop(Id, K, V0),
    (   number(V0)
    ->  aggregate_all(sum(D), (assumed_op(add_prop, E), get_dict(entity, E, Id), get_dict(prop, E, K),
                               get_dict(delta, E, D)), Extra),
        V is V0 + Extra
    ;   V = V0
    ).

base_prop(Id, K, V) :-
    assumed_op(set_prop, E), get_dict(entity, E, Id), get_dict(prop, E, K), get_dict(value, E, V).
base_prop(Id, K, V) :-
    g_prop(Id, K, V), \+ assumed_set(Id, K).
base_prop(Id, K, 0) :-
    assumed_op(add_prop, E), get_dict(entity, E, Id), get_dict(prop, E, K),
    \+ g_prop(Id, K, _), \+ assumed_set(Id, K).

assumed_set(Id, K) :- assumed_op(set_prop, E), get_dict(entity, E, Id), get_dict(prop, E, K).

rel(T, A, B) :- rel(T, A, B, _).
rel(T, A, B, Props) :- g_rel(T, A, B, Props).
rel(T, A, B, Props) :-
    assumed_op(link, E), get_dict(rel, E, T), get_dict(a, E, A), get_dict(b, E, B),
    ( get_dict(props, E, D) -> dict_pairs(D, _, Props) ; Props = [] ).

rel_prop(T, A, B, K, V) :- rel(T, A, B, Props), memberchk(K-V, Props).

now(T) :- g_now(T), !.
now(0).

sees(W, P) :- g_sees(_, _), !, g_sees(W, P).
sees(W, P) :- nonvar(P), catch(py_call(sees, P, L), _, fail), member(W, L).

% 这一拍跨过了周期 P 的边界（每 P 模拟秒一次；跳时间也只算一次）
every(P) :- g_tick(Prev, Now), floor(Now / P) > floor(Prev / P).

assumed(E) :- hyp(E).
assumed_op(Op, E) :- hyp(E), is_dict(E), get_dict(op, E, Op).

% 临时加上一组效果，证明 Goal，再撤掉
assuming(Effects, Goal) :-
    setup_call_cleanup(
        forall(member(E, Effects), assertz(hyp(E))),
        once(Goal),
        forall(member(E, Effects), retract(hyp(E)))).

% 如果事件发生了，Goal 会成立吗
if_done(Event, Goal) :-
    findall(E, after(Event, E), Effects),
    assuming(Effects, Goal).

% ---------------------------------------------------------------------
% 给引擎的出口：结果一律转成 JSON
% ---------------------------------------------------------------------

answers_json(Template, Goal, Json) :-
    findall(Template, Goal, L),
    maplist(to_json, L, J),
    with_output_to(string(Json), json_write_dict(current_output, J, [width(0)])).

to_json(X, X) :- ( number(X) ; string(X) ), !.
to_json(X, X) :- atom(X), !.
to_json(X, J) :- is_dict(X), !, dict_pairs(X, T, Ps), maplist(pair_json, Ps, Js), dict_pairs(J, T, Js).
to_json(X, J) :- is_list(X), !, maplist(to_json, X, J).
to_json(K-V, [KJ, VJ]) :- !, to_json(K, KJ), to_json(V, VJ).
to_json(X, _{functor: F, args: J}) :- compound(X), !, X =.. [F|As], maplist(to_json, As, J).
to_json(X, S) :- term_string(X, S).

pair_json(K-V, K-J) :- to_json(V, J).
