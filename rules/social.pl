% =====================================================================
% 社会规则：敌对、偷窃，以及「做了会怎样」。
%
% 事实每次查询前从 Neo4j 现读（agent/social_rules.py），这里只有规则。
% Neo4j 仍是唯一的事实来源；Prolog 不存任何世界状态。
%
% 读进来的事实（全部是 dynamic，没有就当不成立）：
%   person(P).  alive(P).  player(P).  guard(P).  faction(P, F).
%   trust(P, V).                       P 对玩家的信任（Person.trust_toward_player）
%   feels(A, B, V).                    A 对 B 的好感（FEELS 边）
%   ally(A, B).                        ALLY 边（双向各一条）
%   in_alliance(P, G).  stance(G1, G2, V).
%   attacked_by(A, B, T).              B 在 T 时刻打过 A
%   fighting(A, B, T).                 A 在 T 时刻打过 B（还在打）
%   witnessed(O, C, Crime, Victim, Item).   O 亲眼看见 C 犯了 Crime
%   owns(P, Item).
%   sees(W, P).                        W 现在看得见 P（只在推演后果时给）
%   now(T).                            模拟时钟
%   param(Name, Value).                Python 那边也在用的常量（见 social_rules.PARAMS）
% =====================================================================

:- dynamic person/1, alive/1, player/1, guard/1, faction/2, trust/2, feels/3,
           ally/2, in_alliance/2, stance/3, attacked_by/3, fighting/3,
           witnessed/5, owns/2, sees/2, now/1, param/2, hyp/1.

fact_pred(person/1).      fact_pred(alive/1).       fact_pred(player/1).
fact_pred(guard/1).       fact_pred(faction/2).     fact_pred(trust/2).
fact_pred(feels/3).       fact_pred(ally/2).        fact_pred(in_alliance/2).
fact_pred(stance/3).      fact_pred(attacked_by/3). fact_pred(fighting/3).
fact_pred(witnessed/5).   fact_pred(owns/2).        fact_pred(sees/2).
fact_pred(now/1).         fact_pred(param/2).       fact_pred(hyp/1).

reset_facts :-
    forall(fact_pred(N/A), (functor(H, N, A), retractall(H))).

load_facts(Facts) :-
    reset_facts,
    forall(member(F, Facts), assertz(F)).

% ---------------------------------------------------------------------
% 只在这里用的常量（改规则就改这里）
% ---------------------------------------------------------------------

% 见人就打的阵营
hostile_faction(F) :-
    memberchk(F, [monster, monsters, bandit, bandits, beast, beasts,
                  undead, raider, raiders]).

% 好感到这么低就是仇人
hatred_threshold(-6).

% ---------------------------------------------------------------------
% 好感
% ---------------------------------------------------------------------

% NPC 对玩家的好感就是信任值；其余看 FEELS 边，没有就是 0。
% 推演时 hyp(feeling_delta(A, B, D)) 叠加在上面。
feeling(A, B, V) :-
    base_feeling(A, B, V0),
    aggregate_all(sum(D), hyp(feeling_delta(A, B, D)), Extra),
    V is V0 + Extra.

base_feeling(A, B, V) :- player(B), \+ player(A), !, ( trust(A, V) -> true ; V = 0 ).
base_feeling(A, B, V) :- feels(A, B, V), !.
base_feeling(_, _, 0).

% ---------------------------------------------------------------------
% 敌对：hostile(A, B, Why) —— A 为什么把 B 当敌人（可能有好几个原因）
% Why 的形式：
%   attacked_me(T)                  B 刚打过 A
%   fighting(T)                     A 刚打过 B，还在打
%   faction(FA, FB)                 阵营不同，且有一方见人就打
%   criminal(Crime, Victim, Item)   A 是守卫，看见 B 犯过事
%   hatred(V)                       A 对 B 的好感 ≤ 仇恨线
%   war(GA, GB)                     两人的联盟互相敌对
% 子句顺序就是优先级：hostility/3 取第一个。
% ---------------------------------------------------------------------

hostility(A, B, Why) :- once(hostile(A, B, Why)).

hostile(A, B, Why) :-
    A \== B, alive(A), alive(B),
    reason(A, B, Why).

reason(A, B, attacked_me(T)) :- attacked_by(A, B, T), recent(T).
reason(A, B, fighting(T))    :- fighting(A, B, T), recent(T).
reason(A, B, faction(FA, FB)) :-
    faction(A, FA), faction(B, FB), FA \== FB,
    once(( hostile_faction(FA) ; hostile_faction(FB) )).
reason(A, B, criminal(Crime, Victim, Item)) :-
    guard(A), seen_crime(A, B, Crime, Victim, Item).
reason(A, B, hatred(V)) :-
    feeling(A, B, V), hatred_threshold(H), V =< H.
reason(A, B, war(GA, GB)) :-
    in_alliance(A, GA), in_alliance(B, GB), GA \== GB,
    stance(GA, GB, S), param(alliance_hostile, H), S =< H.

recent(T) :- now(Now), param(combat_memory, M), Now - T =< M.

seen_crime(O, C, Crime, Victim, Item) :- witnessed(O, C, Crime, Victim, Item).
seen_crime(O, C, Crime, Victim, Item) :- hyp(witnessed(O, C, Crime, Victim, Item)).

% ---------------------------------------------------------------------
% 偷窃：拿走一件还活着的别人的东西，而且主人不当你是盟友
% ---------------------------------------------------------------------

theft(Taker, Item, Owner) :-
    owns(Owner, Item), Owner \== Taker, alive(Owner),
    \+ ally(Owner, Taker).

% ---------------------------------------------------------------------
% 推演：如果 Actor 拿走 Item，会怎样（不改任何事实）
%   consequence(take(Actor, Item), theft(Victim))
%   consequence(take(Actor, Item), turns_hostile(W, Why))   原来不敌视、之后会敌视的人
% 看见的人会记下偷窃；失主好感 param(theft_victim)，旁观者 param(theft_witness)。
% ---------------------------------------------------------------------

consequence(take(Actor, Item), theft(Victim)) :-
    theft(Actor, Item, Victim).
consequence(take(Actor, Item), turns_hostile(W, Why)) :-
    theft(Actor, Item, Victim),
    sees(W, Actor), W \== Actor,
    \+ hostile(W, Actor, _),
    take_effects(W, Actor, Victim, Item, Effects),
    assuming(Effects, hostility(W, Actor, Why)).

take_effects(W, Actor, Victim, Item, [witnessed(W, Actor, theft, Victim, Item) | Feel]) :-
    (   player(W) -> Feel = []                       % 玩家的感受由玩家自己决定
    ;   W == Victim -> param(theft_victim, D), Feel = [feeling_delta(W, Actor, D)]
    ;   param(theft_witness, D), Feel = [feeling_delta(W, Actor, D)]
    ).

% 临时加上一些假设，证明 Goal，然后撤掉
assuming(Hyps, Goal) :-
    setup_call_cleanup(
        forall(member(H, Hyps), assertz(hyp(H))),
        once(Goal),
        forall(member(H, Hyps), retract(hyp(H)))).
