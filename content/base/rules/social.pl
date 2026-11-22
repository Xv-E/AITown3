:- encoding(utf8).
% =====================================================================
% base 包：敌对与偷窃。
%
% 只通过引擎核心的读图谓词（node / prop / rel / rel_prop / now / assumed / sees）
% 读世界，见 agent/rules_core.pl。这里定义的东西引擎都不认识，只会照着问：
%   hostility(A, B, Why)    combat 问「A 为什么把 B 当敌人」
%   after(Event, Effect)    动作做完后要发生的效果（op 格式同动作 JSON）
%   forecast(Event, Text)   做之前给 NPC 的提示
%   label/2  explain/4      原因的标签和一句话解释
% =====================================================================

% 其他包可以接着加敌对原因、阵营、标签和解释（加在后面的原因优先级更低）
:- multifile reason/3, hostile_faction/1.
:- discontiguous reason/3, hostile_faction/1.

reads(label('Person')).
reads(label('Alliance')).
reads(rel('FEELS')).
reads(rel('ALLY')).
reads(rel('MEMBER_OF')).
reads(rel('STANCE')).
reads(rel('WITNESSED')).
reads(rel('OWNS')).

% ---------------------------------------------------------------------
% 常量
% ---------------------------------------------------------------------

const(combat_memory, 25.0).     % 模拟秒（两个游戏小时）：这么久之内打过你的人还是敌人
const(theft_victim, -5).        % 失主亲眼看见被偷：好感
const(theft_witness, -2).       % 旁观者看见偷窃：好感
const(alliance_hostile, -5).    % 联盟之间的立场到这么低就是敌对

hostile_faction(F) :-
    memberchk(F, [monster, monsters, bandit, bandits, beast, beasts, undead, raider, raiders]).

hatred_threshold(-6).

% 好感的范围（与 relations.adjust / npc_knowledge 的取值范围一致）
feeling_range(npc, -10, 10).
feeling_range(player_trust, -5, 10).

% ---------------------------------------------------------------------
% 词汇
% ---------------------------------------------------------------------

person(P) :- node('Person', P).
alive(P) :- person(P), \+ prop(P, dead, true), prop(P, hp, HP), HP > 0.
player(P) :- once(( prop(P, is_player, true) ; prop(P, role, player) )).
guard(P) :- prop(P, guard, true).
faction(P, F) :- person(P), ( prop(P, faction, F0) -> F = F0 ; F = '' ).
ally(A, B) :- rel('ALLY', A, B).
owner(Item, O) :- rel('OWNS', O, Item).
in_alliance(P, G) :- rel('MEMBER_OF', P, G), node('Alliance', G).
stance(GA, GB, S) :- rel_prop('STANCE', GA, GB, value, S).
recent(T) :- now(Now), const(combat_memory, M), Now - T =< M.

% NPC 对玩家的好感就是信任值；其余看 FEELS 边，没有就是 0。推演里的 feel 效果叠加上去。
feeling(A, B, V) :-
    base_feeling(A, B, V0, Range),
    aggregate_all(sum(D), (assumed_op(feel, E), get_dict(a, E, A), get_dict(b, E, B), get_dict(delta, E, D)), X),
    (   X =:= 0 -> V = V0                               % 存着的值原样用
    ;   feeling_range(Range, Lo, Hi), V is max(Lo, min(Hi, V0 + X))   % 改动后的值和真改一样夹在范围里
    ).

base_feeling(A, B, V, player_trust) :- player(B), \+ player(A), !, ( prop(A, trust_toward_player, V) -> true ; V = 0 ).
base_feeling(A, B, V, npc) :- rel_prop('FEELS', A, B, value, V), !.
base_feeling(_, _, 0, npc).

% ---------------------------------------------------------------------
% 敌对：子句顺序就是优先级，hostility/3 取第一个
% ---------------------------------------------------------------------

hostility(A, B, Why) :- once(hostile(A, B, Why)).

hostile(A, B, Why) :- A \== B, alive(A), alive(B), reason(A, B, Why).

reason(A, B, attacked_me(T)) :- prop(A, attacked_by, B), prop(A, attacked_at, T), recent(T).
reason(A, B, fighting(T))    :- prop(A, fighting, B), prop(A, fighting_at, T), recent(T).
reason(A, B, faction(FA, FB)) :-
    faction(A, FA), faction(B, FB), FA \== FB,
    once(( hostile_faction(FA) ; hostile_faction(FB) )).
reason(A, B, criminal(Crime, Victim, Item)) :-
    guard(A), rel('WITNESSED', A, B, Ps),
    pv(Ps, crime, Crime), pv(Ps, victim, Victim), pv(Ps, item, Item).
reason(A, B, hatred(V)) :- feeling(A, B, V), hatred_threshold(H), V =< H.
reason(A, B, war(GA, GB)) :-
    in_alliance(A, GA), in_alliance(B, GB), GA \== GB,
    stance(GA, GB, S), const(alliance_hostile, H), S =< H.

pv(Ps, K, V) :- ( memberchk(K-V0, Ps) -> V = V0 ; V = '' ).

label(attacked_me(_), 'attacked me').
label(fighting(_), fighting).
label(faction(_, _), faction).
label(criminal(_, _, _), criminal).
label(hatred(_), hatred).
label(war(_, _), war).

explain(attacked_me(_), A, B, T) :- format(atom(T), "~w attacked ~w recently", [B, A]).
explain(fighting(_), A, B, T) :- format(atom(T), "~w is in a fight with ~w", [A, B]).
explain(faction(FA, FB), A, B, T) :-
    side(FA, SA), side(FB, SB),
    format(atom(T), "~w (~w) and ~w (~w) are on sides that fight on sight", [A, SA, B, SB]).
explain(criminal(Crime, Victim, Item), A, B, T) :-
    (   Victim \== '', Item \== '' -> format(atom(W), "~w (~w's ~w)", [Crime, Victim, Item])
    ;   Victim \== '' -> format(atom(W), "~w against ~w", [Crime, Victim])
    ;   W = Crime
    ),
    format(atom(T), "~w is a guard and saw ~w commit ~w", [A, B, W]).
explain(hatred(V), A, B, T) :- format(atom(T), "~w hates ~w (feeling ~w)", [A, B, V]).
explain(war(GA, GB), A, B, T) :- format(atom(T), "~w's alliance ~w is hostile to ~w's alliance ~w", [A, GA, B, GB]).

side('', 'no faction') :- !.
side(F, F).

% ---------------------------------------------------------------------
% 偷窃：拿走一件还活着的别人的东西，而且主人不当你是盟友
% ---------------------------------------------------------------------

theft(Taker, Item, Owner) :-
    owner(Item, Owner), Owner \== Taker, alive(Owner), \+ ally(Owner, Taker).

% 拿走（take 能力）之后：看见的人记下来、好感下降、告诉做的人
after(take(A, I, _), E) :- theft(A, I, V), now(T), stolen(A, I, V, T, E).

stolen(A, I, V, T, _{op: link, a: W, a_label: 'Person', b: A, b_label: 'Person', rel: 'WITNESSED',
                     props: _{crime: theft, victim: V, item: I, at: T}}) :-
    sees(W, A).
stolen(A, _, _, _, _{op: add_prop, entity: A, label: 'Person', prop: crimes, delta: 1}) :-
    once(sees(_, A)).
stolen(A, I, V, T, _{op: feel, a: W, b: A, delta: D, reason: R, at: T}) :-
    sees(W, A), \+ player(W),
    (   W == V -> const(theft_victim, D), format(atom(R), "stole my ~w", [I])
    ;   const(theft_witness, D), format(atom(R), "saw them steal ~w's ~w", [V, I])
    ).
stolen(A, _, V, _, _{op: report, theft: true, victim: V, witnesses: Ws}) :-
    findall(W, sees(W, A), Ws).
stolen(A, _, V, _, _{op: note, text: Text}) :-
    findall(W, sees(W, A), Ws),
    (   Ws == [] -> format(atom(Text), "you stole from ~w; nobody saw", [V])
    ;   atomic_list_concat(Ws, ', ', S), format(atom(Text), "you stole from ~w; seen by ~w", [V, S])
    ).

% 拿之前：算不算偷、谁会因此翻脸
forecast(take(A, I, _), "taking it is stealing") :- theft(A, I, _).
forecast(take(A, I, _), "you may take it (not stealing)") :- owner(I, O), O \== A, \+ theft(A, I, _).
forecast(take(A, I, L), Text) :-
    theft(A, I, _), sees(W, A), \+ hostile(W, A, _),
    if_done(take(A, I, L), hostility(W, A, Why)),
    explain(Why, W, A, E),
    format(atom(Text), "then ~w would turn hostile (~w)", [W, E]).
