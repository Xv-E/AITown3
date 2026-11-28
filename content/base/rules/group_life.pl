:- encoding(utf8).
% =====================================================================
% base 包：群体随时间的变化。
%
% 盟友和朋友结的伙（每两个游戏小时，见 time.pl 的 decay_every）：
%   bonds    互相好感都够高就成了盟友（ALLY），并聚成一伙（kind: band 的群体）；好感跌破 0 的盟约破裂
%   leave    mood_bound 的群体里，不再喜欢同伴的人退出
%   dissolve 人太少又没地盘的群体解散（enduring 的不会）
%   mood     两个 mood_bound 群体之间的立场 = 两边成员互相好感的平均
%
% 群体演化（每 group_tick_hours 游戏小时）：
%   territory  有成员在场的地点控制力上升（忠诚越高越快），没人看着的慢慢流失；对头同处一地互相削弱
%   control    每处的控制者 = 控制力最高且 ≥ 1 的群体（写到 Location.controlled_by）
%   tension    共处一地的群体积累张力，满了立场恶化一级；友好共处张力下降，降到底立场改善一级
%   loyalty    站在自家地盘上忠诚上升；丢地盘、身处敌对地盘、和首领不和则下降
%   emerge     （有冷却）分裂、结盟另立门户、外来者进驻 —— 名字和来历交给扩展 new_group_story（LLM）
%   recruit    控制某地的群体吸收站在那里、对原群体不再忠诚的人（每轮最多一人）
%   rise       成员和地盘多了升格
% 大事写进编年史（:FactionEvent）、进新闻、记进相关成员的记忆（informal 的群体不记）。
% =====================================================================

:- discontiguous tick/2, after/2.

reads(label('WorldMeta', [group_last_emerge_at])).
reads(label('Person', [bonds])).
reads(rel('FRIENDS_WITH')).
reads(rel('LIKES')).
reads(rel('FAMILY_OF')).

const(auto_ally, 6).          % 互相好感都到这么高就自然成了盟友
const(ally_min, 4).           % 提出结盟时，对方对你的好感至少这么多
const(stance_friendly, 4).    % 好感平均到这么高：友好
const(stance_rival, -2).      % 到这么低：对立
const(stance_hostile, -5).    % 到这么低：敌对
const(group_tick_hours, 6.0).
const(claim_rate, 0.6).
const(decay, 0.3).
const(contest_rate, 0.5).
const(control_min, 1.0).
const(strength_max, 10.0).
const(tension_step, 1.0).
const(tension_relax, 0.5).
const(tension_decay, 0.25).
const(escalate_at, 6.0).
const(splinter_loyalty, 3.0).
const(coalition_loyalty, 5.0).
const(recruit_limit, 4.0).
const(emerge_cooldown, 4).    % 轮
const(max_groups, 12).
const(arrival_chance, 0.15).

ladder([hostile, rival, neutral, friendly, allied]).
size_scale(N, large) :- N >= 6, !.
size_scale(N, medium) :- N >= 3, !.
size_scale(_, small).

group_period(P) :- const(group_tick_hours, H), day_length(L), P is H * L / 24.
tick_period(P) :- group_period(P).

band_due :- const(decay_every, P), every(P).
group_due :- group_period(P), every(P).

tick_phase(10, bonds).
tick_phase(20, leave).
tick_phase(30, dissolve).
tick_phase(40, mood).
tick_phase(100, territory).
tick_phase(110, control).
tick_phase(120, tension).
tick_phase(130, loyalty).
tick_phase(140, emerge).
tick_phase(150, recruit).
tick_phase(160, rise).

% ---------------------------------------------------------------------
% 盟友与朋友结的伙
% ---------------------------------------------------------------------

would_ally(Other, Me) :- feeling(Other, Me, V), const(ally_min, M), V >= M.

band_of(P, G) :- once(( direct(P, G), has(G, mood_bound) )).

% 两人所在的群体彼此敌对，而且群体记仇（买卖、结盟的动作条件用）
at_war(A, B) :- direct(A, GA), direct(B, GB), GA \== GB, stance(GA, GB, hostile), once(( has(GA, feud) ; has(GB, feud) )).

stance_word(V, friendly) :- const(stance_friendly, F), V >= F, !.
stance_word(V, hostile) :- const(stance_hostile, H), V =< H, !.
stance_word(V, rival) :- const(stance_rival, R), V =< R, !.
stance_word(_, neutral).

% 立场写成一条（已有哪个方向就沿用哪个方向）
stance_effects(A, B, S, Reason, T, Es) :-
    Es = [ _{op: unlink, a: A, a_label: 'Faction', b: B, b_label: 'Faction', rel: 'FACTION_REL'},
           _{op: unlink, a: B, a_label: 'Faction', b: A, b_label: 'Faction', rel: 'FACTION_REL'},
           _{op: link, a: A, a_label: 'Faction', b: B, b_label: 'Faction', rel: 'FACTION_REL',
             props: _{stance: S, reason: Reason, tension: T}} ].
rel_dir(A, B, X, Y) :- ( rel('FACTION_REL', B, A, _), \+ rel('FACTION_REL', A, B, _) -> X = B, Y = A ; X = A, Y = B ).
rel_prop_effect(A, B, K, V, _{op: set_rel_prop, a: X, a_label: 'Faction', b: Y, b_label: 'Faction', rel: 'FACTION_REL',
                              prop: K, value: V}) :- rel_dir(A, B, X, Y).

% a、b 刚结盟：都没有伙就新成立一个；一方有就把另一方拉进来；两边各有伙就让两伙更亲近
join_effect(A, B, _, E) :-
    band_of(A, GA), band_of(B, GB), !, GA \== GB,
    ( group_rel(GA, GB, Ps), kv(Ps, value, 0, V0) -> true ; V0 = 0 ),
    const(stance_friendly, F), V is max(V0, F), stance_word(V, W),
    (   group_rel(GA, GB, _)
    ->  ( rel_prop_effect(GA, GB, value, V, E) ; rel_prop_effect(GA, GB, stance, W, E) )
    ;   E = _{op: link, a: GA, a_label: 'Faction', b: GB, b_label: 'Faction', rel: 'FACTION_REL',
              props: _{stance: W, value: V, reason: "their members are allies", tension: 0}}
    ).
join_effect(A, B, _, E) :- band_of(A, G), !, member_link(B, G, E).
join_effect(A, B, _, E) :- band_of(B, G), !, member_link(A, G, E).
join_effect(A, B, T, E) :-
    format(atom(G), "~w's Band", [A]),
    (   E = _{op: merge_node, label: 'Faction', name: G, props: _{kind: band, scale: small, status: active, founded_at: T}}
    ;   E = _{op: set_prop, entity: G, label: 'Faction', prop: status, value: active}
    ;   member_link(A, G, E)
    ;   member_link(B, G, E)
    ).

member_link(P, G, _{op: link, a: P, a_label: 'Person', b: G, b_label: 'Faction', rel: 'MEMBER_OF'}).
ally_link(A, B, _{op: link, a: A, a_label: 'Person', b: B, b_label: 'Person', rel: 'ALLY'}).

tick(bonds, E) :-
    band_due, new_allies(A, B), now(T),
    ( ally_link(A, B, E) ; ally_link(B, A, E) ; join_effect(A, B, T, E) ).
tick(bonds, _{op: unlink, a: A, a_label: 'Person', b: B, b_label: 'Person', rel: 'ALLY'}) :-
    band_due, ally(A, B), rel_prop('FEELS', A, B, value, V), V < 0.

new_allies(A, B) :-
    const(auto_ally, M),
    rel_prop('FEELS', A, B, value, V1), V1 >= M, A @< B,
    rel_prop('FEELS', B, A, value, V2), V2 >= M,
    \+ ally(A, B).

tick(leave, _{op: unlink, a: P, a_label: 'Person', b: G, b_label: 'Faction', rel: 'MEMBER_OF'}) :-
    band_due, group(G), has(G, mood_bound), direct(P, G),
    findall(V, (direct(O, G), O \== P, feeling(P, O, V)), Vs), Vs \== [],
    sum_list(Vs, S), S < 0.

tick(dissolve, E) :-
    ( band_due ; group_due ), group(G), \+ has(G, enduring),
    traits(G, Ts), ( member(min_members(Min), Ts) -> true ; Min = 1 ),
    aggregate_all(count, (direct(P, G), living(P)), N), N < Min, \+ holds_ground(G),
    now(T), format(atom(Txt), "~w faded away: no members left, no ground held.", [G]),
    (   E = _{op: set_prop, entity: G, label: 'Faction', prop: status, value: dissolved}
    ;   E = _{op: set_prop, entity: G, label: 'Faction', prop: dissolved_at, value: T}
    ;   chronicle(dissolved, Txt, [G], '', E)
    ).

tick(mood, E) :-
    band_due, group(GA), has(GA, mood_bound), group(GB), GA @< GB, has(GB, mood_bound),
    findall(V, (direct(X, GA), direct(Y, GB), ( feeling(X, Y, V) ; feeling(Y, X, V) )), Vs),
    Vs \== [], sum_list(Vs, S), length(Vs, N),
    M is max(-10, min(10, round(S / N))), stance_word(M, W),
    (   group_rel(GA, GB, _)
    ->  ( rel_prop_effect(GA, GB, value, M, E) ; rel_prop_effect(GA, GB, stance, W, E) )
    ;   E = _{op: link, a: GA, a_label: 'Faction', b: GB, b_label: 'Faction', rel: 'FACTION_REL',
              props: _{stance: W, value: M, reason: "how their members feel about each other", tension: 0}}
    ).

% ---------------------------------------------------------------------
% 编年史
% ---------------------------------------------------------------------

% 一件大事：编年史节点、各群体的近况、新闻、相关成员的记忆。全是 informal 的群体就不记。
chronicle(_, _, Gs, _, _) :- Gs \== [], forall(member(G, Gs), has(G, informal)), !, fail.
chronicle(Kind, Text, Gs, Loc, E) :-
    now(T), day(T, D0), D is D0 + 1, term_hash(Text, H), format(atom(N), "FactionEvent_~0f_~w", [T * 100, H]),
    (   E = _{op: merge_node, label: 'FactionEvent', name: N,
              props: _{t: T, day: D, kind: Kind, text: Text, factions: Gs, location: Loc}}
    ;   member(G, Gs), E = _{op: link, a: G, a_label: 'Faction', b: N, b_label: 'FactionEvent', rel: 'IN_EVENT'}
    ;   member(G, Gs), E = _{op: set_prop, entity: G, label: 'Faction', prop: last_news, value: Text}
    ;   member(G, Gs), E = _{op: set_prop, entity: G, label: 'Faction', prop: last_news_day, value: D}
    ;   E = _{op: announce, text: Text, source: factions}
    ;   setof(P, G^(member(G, Gs), direct(P, G)), Ps), member(P, Ps),
        format(atom(M), "[Faction news, day ~w] ~w", [D, Text]),
        E = _{op: remember, who: P, text: M, type: faction_event, location: Loc}
    ).

% ---------------------------------------------------------------------
% 地盘
% ---------------------------------------------------------------------

% 只住人的地方（标签只有 home 的室内地点）不算地盘：算到楼外那块地上
private_home(L) :- prop(L, tags, Tags), atom(Tags), split_string(Tags, ",", " ", Parts0),
    exclude(==(""), Parts0, Parts), Parts \== [], forall(member(X, Parts), (string_lower(X, "home"))).
public_place(L, P) :- ( private_home(L), prop(L, parent, P0), P0 \== '' -> P = P0 ; P = L ).

% 群体 G 在 L 的在场力量：每个活着的直接成员按忠诚算（新人减半）；外来者有一群没名字的人守着
presence(G, L, P) :-
    aggregate_all(sum(W), ( direct(M, G, Role, Loy), living(M), location(M, L), \+ private_home(L),
                            ( Role == recruit -> K = 0.5 ; K = 1.0 ), W is K * Loy / 10 ), P0),
    ( prop(G, offscreen, Off), number(Off), Off > 0, strength(G, L, S), S > 0 -> P is P0 + Off ; P = P0 ).

present_pair(G, L) :- direct(M, G), group(G), living(M), location(M, L), \+ private_home(L).
present_pair(G, L) :- prop(G, offscreen, Off), number(Off), Off > 0, strength(G, L, S), S > 0, group(G).

holds_ground(G) :- const(control_min, Min), strength(G, L, S), S >= Min, node('Location', L), !.

% 这一轮之后的控制力（还没算对头的削弱）
grown(G, L, S1) :-
    ( strength(G, L, S0) -> true ; S0 = 0.0 ), presence(G, L, P),
    (   P > 0 -> const(claim_rate, R), mult(G, claim, M), S1 is S0 + P * R * M
    ;   has(G, landed) -> S1 = S0                       % 名下的地（据点）：没人看着也不流失
    ;   const(decay, D), S1 is S0 - D
    ).

territory_keys(Ks) :-
    findall(G-L, ( strength(G, L, _), group(G) ; present_pair(G, L) ), K0), sort(K0, Ks).

tick(territory, E) :-
    group_due, territory_keys(Keys),
    findall(G-L-S, (member(G-L, Keys), grown(G, L, S)), Grown),
    member(G-L, Keys), memberchk(G-L-S1, Grown),
    findall(H, ( member(O-L-SO, Grown), O \== G, SO > 0, S1 > 0, stance(G, O, St), foe(St),
                 const(contest_rate, CR), ( St == hostile -> K = 2.0 ; K = 1.0 ),
                 presence(O, L, PO), mult(O, contest, MC), H is CR * K * PO * MC ), Hits),
    sum_list(Hits, Hit), const(strength_max, Max),
    S2 is max(0.0, min(Max, S1 - Hit)),
    ( strength(G, L, S0) -> true ; S0 = 0.0 ),
    abs(S2 - S0) > 1.0e-6,
    (   S2 =< 0.05
    ->  E = _{op: unlink, a: G, a_label: 'Faction', b: L, b_label: 'Location', rel: 'CONTROLS'}
    ;   S3 is round(S2 * 100) / 100,
        E = _{op: set_rel_prop, a: G, a_label: 'Faction', b: L, b_label: 'Location', rel: 'CONTROLS', prop: strength, value: S3}
    ;   const(control_min, Min), S0 >= Min, S2 < Min, now(T),
        E = _{op: set_prop, entity: G, label: 'Faction', prop: lost_ground_at, value: T}
    ).

% 每处的控制者
best_controller(L, G) :-
    const(control_min, Min),
    findall(S-G0, (strength(G0, L, S), group(G0), S >= Min), Ks), Ks \== [],
    max_member(_-G, Ks).

control_change(L, Old, New) :-
    node('Location', L), ( controller(L, Old0) -> Old = Old0 ; Old = '' ),
    ( best_controller(L, New0) -> New = New0 ; New = '' ), Old \== New.

control_effect(L, _, New, _{op: set_prop, entity: L, label: 'Location', prop: controlled_by, value: V}) :-
    ( New == '' -> V = null ; V = New ).

tick(control, E) :-
    group_due, control_change(L, Old, New),
    (   control_effect(L, Old, New, E)
    ;   (   New \== '', Old \== '' -> format(atom(T), "~w drove ~w out of ~w.", [New, Old, L]), Gs = [New, Old]
        ;   New \== '' -> format(atom(T), "~w took hold of ~w.", [New, L]), Gs = [New]
        ;   format(atom(T), "~w lost its hold on ~w; nobody controls it now.", [Old, L]), Gs = [Old]
        ),
        chronicle(territory, T, Gs, L, E)
    ).

% 新世界建好：先把控制者写上（不进编年史）
after(world_built, E) :- control_change(L, Old, New), control_effect(L, Old, New, E).

% ---------------------------------------------------------------------
% 张力与立场
% ---------------------------------------------------------------------

nested(A, B) :- ( above(A, B, _) ; above(B, A, _) ), !.

shared_place(A, B, L) :-
    setof(L0, S1^S2^(strength(A, L0, S1), S1 > 0, strength(B, L0, S2), S2 > 0), [L|_]).

tension_pair(A, B) :-
    setof(X-Y, X0^Y0^P^S1^S2^L^( ( rel('FACTION_REL', X0, Y0, P) ; strength(X0, L, S1), S1 > 0, strength(Y0, L, S2), S2 > 0 ),
                           X0 \== Y0, group(X0), group(Y0), msort([X0, Y0], [X, Y]) ), Pairs),
    member(A-B, Pairs), \+ nested(A, B), \+ ( has(A, mood_bound), has(B, mood_bound) ).

tick(tension, E) :-
    group_due, tension_pair(A, B),
    ( group_rel(A, B, Ps) -> Rel = yes, kv(Ps, stance, neutral, St), kv(Ps, tension, 0.0, T0), kv(Ps, reason, '', R0)
    ; Rel = no, St = neutral, T0 = 0.0, R0 = '' ),
    ( shared_place(A, B, L) -> true ; L = '' ),
    (   L \== '', memberchk(St, [allied, friendly]) -> const(tension_relax, K), T1 is T0 - K
    ;   L \== '' -> const(tension_step, K), mult(A, escalate, EA), mult(B, escalate, EB), T1 is T0 + K * (EA + EB) / 2
    ;   const(tension_decay, K), ( T0 > 0 -> T1 is T0 - K ; T1 is min(0.0, T0 + K) )
    ),
    \+ ( Rel == no, L == '' ),
    ladder(Lad), ( nth0(I, Lad, St) -> true ; I = 2 ), length(Lad, NL), const(escalate_at, Esc),
    (   T1 >= Esc, I > 0
    ->  I1 is I - 1, nth0(I1, Lad, New),
        ( L \== '' -> format(atom(Why), "clashing over ~w", [L]) ; Why = R0 ),
        format(atom(Txt), "~w and ~w went from ~w to ~w, ~w.", [A, B, St, New, Why]),
        ( stance_effects(A, B, New, Why, 0.0, Es), member(E, Es) ; chronicle(stance, Txt, [A, B], L, E) )
    ;   T1 =< -Esc, I < NL - 1
    ->  I1 is I + 1, nth0(I1, Lad, New),
        ( L \== '' -> format(atom(Why), "standing side by side at ~w", [L]) ; Why = R0 ),
        format(atom(Txt), "~w and ~w grew closer: ~w → ~w, ~w.", [A, B, St, New, Why]),
        ( stance_effects(A, B, New, Why, 0.0, Es), member(E, Es) ; chronicle(stance, Txt, [A, B], L, E) )
    ;   Rel == no
    ->  format(atom(Why), "both claim ~w", [L]), stance_effects(A, B, neutral, Why, T1, Es), member(E, Es)
    ;   T2 is round(T1 * 100) / 100, rel_prop_effect(A, B, tension, T2, E)
    ).

% ---------------------------------------------------------------------
% 忠诚
% ---------------------------------------------------------------------

tick(loyalty, _{op: set_rel_prop, a: P, a_label: 'Person', b: G, b_label: 'Faction', rel: 'MEMBER_OF',
                prop: loyalty, value: L1}) :-
    group_due, direct(P, G, _, L0), living(P), \+ player(P),
    sum(G, loyalty, D0),
    ( location(P, Here), controller(Here, C) -> true ; C = '' ),
    (   C == G -> D1 is D0 + 0.15
    ;   \+ has(G, homebound), holds_ground(G)
    ->  ( C \== '', stance(G, C, hostile) -> D1 is D0 - 0.3 ; D1 is D0 - 0.1 )
    ;   D1 = D0
    ),
    ( now(T), prop(G, lost_ground_at, T) -> D2 is D1 - 0.5 ; D2 = D1 ),
    ( leader_of(G, Lead), Lead \== P, attitude(P, Lead, S, _), S < 0 -> D is D2 - 0.2 ; D = D2 ),
    abs(D) > 1.0e-6,
    L1 is round(max(0.0, min(10.0, L0 + D)) * 100) / 100.

% ---------------------------------------------------------------------
% 新群体：分裂、结盟另立门户、外来者进驻（交给扩展 new_group_story 写名字和来历）
% ---------------------------------------------------------------------

tick(emerge, E) :-
    group_due, emerge_ready,
    once(emergence(Ctx)), now(T),
    (   E = _{op: merge_node, label: 'WorldMeta', name: 'WorldMeta', props: _{}}
    ;   E = _{op: set_prop, entity: 'WorldMeta', label: 'WorldMeta', prop: group_last_emerge_at, value: T}
    ;   E = _{op: extend, name: new_group_story, ctx: Ctx}
    ).

emerge_ready :-
    now(T), group_period(P), const(emerge_cooldown, N),
    ( prop('WorldMeta', group_last_emerge_at, Last) -> T - Last >= N * P - 1.0e-6 ; true ),
    aggregate_all(count, group(_), Count), const(max_groups, Max), Count < Max.

emergence(_{type: splinter, parent: G, parent_kind: K, founders: [F|Fs], location: Pub}) :-
    const(splinter_loyalty, Low),
    findall(L-F0-G0, ( direct(F0, G0, Role, L), Role \== recruit, L =< Low, living(F0), \+ player(F0),
                       \+ has(G0, broad), \+ has(G0, informal) ), Cands),
    msort(Cands, Sorted), member(_-F-G, Sorted),
    kind(G, K), Lim is Low + 1.5,
    findall(X, ( direct(X, G, _, LX), X \== F, LX =< Lim, living(X), \+ player(X),
                 attitude(X, F, S, _), S >= 1.0 ), Xs0), sort(Xs0, Xs), top(2, Xs, Fs),
    ( location(F, Loc) -> public_place(Loc, Pub) ; Pub = '' ).
emergence(_{type: coalition, founders: [A, B], location: Pub, from: From}) :-
    const(coalition_loyalty, Max),
    findall(P, (direct(P, G), \+ has(G, homebound), living(P), \+ player(P)), Ps0), sort(Ps0, Ps),
    member(A, Ps), member(B, Ps), A @< B,
    location(A, Loc), location(B, Loc),
    \+ ( direct(A, G), direct(B, G) ),
    best_loyalty(A, LA), LA =< Max, best_loyalty(B, LB), LB =< Max,
    attitude(A, B, SA, _), SA >= 0, attitude(B, A, SB, _), SB >= 0,
    personally_close(A, B),
    findall(G, ((direct(A, G) ; direct(B, G)), \+ has(G, homebound)), F0), sort(F0, From),
    public_place(Loc, Pub).
emergence(_{type: arrival, founders: [], location: L}) :-
    findall(L0, (node('Location', L0), \+ controller(L0, _), \+ private_home(L0), \+ within_held(L0)), Free0), sort(Free0, Free),
    Free \== [], const(arrival_chance, C), chance(arrival, C),
    now(T), term_hash(T, H), length(Free, N), I is H mod N, nth0(I, Free, L).

% 别人地盘里的室内房间（据点的农舍、帮派楼里的屋子）不算空地
within_held(L) :- prop(L, parent, Out), Out \== '', controller(Out, _).

best_loyalty(P, L) :- aggregate_all(max(L0), (direct(P, G, _, L0), \+ has(G, homebound)), L).

personally_close(A, B) :- member(T, ['FRIENDS_WITH', 'LIKES', 'FAMILY_OF']), ( rel(T, A, B) ; rel(T, B, A) ), !.
personally_close(A, B) :- bonded(A, B), bonded(B, A).
bonded(A, B) :- prop(A, bonds, Raw), atom(Raw), catch(atom_json_dict(Raw, D, []), _, fail), is_dict(D),
    atom_string(B, S), dict_pairs(D, _, Ps), member(K-_, Ps), atom_string(K, S), !.

% 没有 LLM 时的故事（扩展先拿这个，LLM 写得好就替换）
arrival_name(N) :- member(N, ['AshenBand', 'GreyRiders', 'SaltTraders', 'LanternOrder', 'ThornKin',
                              'RiverWardens', 'IronPilgrims', 'DuskCompany']).
arrival_trait(T) :- member(T, [expansionist, militant, mercantile, zealous, secretive]).

splinter_kind(gang, gang). splinter_kind(crew, gang). splinter_kind(authority, order).
splinter_kind(family, family). splinter_kind(guild, crew). splinter_kind(town, crew).

story(Ctx, _{name: N, kind: K, goal: Goal, description: D, traits: Ts, relations: Rs, chronicle: C}) :-
    Ctx.type == splinter, !,
    Parent = Ctx.parent, [Lead|Rest] = Ctx.founders, Loc0 = Ctx.location,
    ( Loc0 == '' -> Loc = 'the wilds' ; Loc = Loc0 ),
    ( splinter_kind(Ctx.parent_kind, K) -> true ; K = crew ),
    atom_concat('Free', Parent, Free),
    ( node(_, Free) -> atom_concat(Lead, 'Loyalists', N) ; N = Free ),
    format(atom(Goal), "Prove that ~w was wrong to cast them aside", [Parent]),
    ( Rest == [] -> Along = 'nobody but their pride' ; atomic_list_concat(Rest, ', ', Along) ),
    format(atom(D), "~w walked out of ~w at ~w, sick of how they were treated, and took ~w along.", [Lead, Parent, Loc, Along]),
    format(atom(W1), "born from ~w's grudge against ~w", [Lead, Parent]),
    Ts = [_{id: vengeful, why: W1}, _{id: insular, why: "they trust only those who walked out together"}],
    format(atom(RW), "~w broke away in anger", [Lead]),
    Rs = [_{with: Parent, stance: rival, reason: RW}],
    format(atom(C), "~w broke away from ~w and founded a faction of their own.", [Lead, Parent]).
story(Ctx, _{name: N, kind: crew, goal: "Stand by each other whatever their old sides demand", description: D,
             traits: Ts, relations: Rs, chronicle: C}) :-
    Ctx.type == coalition, !,
    [A, B|_] = Ctx.founders, atomic_list_concat([A, B, 'Pact'], N),
    ( Ctx.from == [] -> Old = 'their old sides' ; atomic_list_concat(Ctx.from, ', ', Old) ),
    format(atom(D), "~w and ~w came from different camps but found more loyalty in each other than in ~w.", [A, B, Old]),
    Ts = [_{id: welcoming, why: "founded by people who know how it feels to be outsiders"},
          _{id: mercantile, why: "they hold together by trading favours across old lines"}],
    findall(_{with: F, stance: neutral, reason: "a founder left their ranks"}, member(F, Ctx.from), Rs),
    format(atom(C), "~w and ~w swore a pact at ~w, beholden to neither of their old sides.", [A, B, Ctx.location]).
story(Ctx, _{name: N, kind: K, goal: Goal, description: D, traits: Ts, relations: [], chronicle: C}) :-
    Loc = Ctx.location, now(T), term_hash(T-Loc, H),
    findall(X, (arrival_name(X), \+ node(_, X)), Names),
    ( Names == [] -> N = 'Newcomers' ; length(Names, NN), I is H mod NN, nth0(I, Names, N) ),
    findall(X, arrival_trait(X), Pool), length(Pool, NP),
    I1 is H mod NP, I2 is (I1 + 1 + (H // 7) mod (NP - 1)) mod NP, nth0(I1, Pool, T1), nth0(I2, Pool, T2),
    Kinds = [gang, order, guild], I3 is (H // 13) mod 3, nth0(I3, Kinds, K),
    format(atom(Goal), "Make ~w their own", [Loc]),
    format(atom(D), "Strangers came down the road and settled at ~w, where nobody held sway.", [Loc]),
    format(atom(Why), "how they meant to survive after arriving at ~w", [Loc]),
    Ts = [_{id: T1, why: Why}, _{id: T2, why: Why}],
    format(atom(C), "Newcomers calling themselves ~w settled at ~w.", [N, Loc]).

% 故事写好了：把新群体写进世界
founding(Ctx, Story, E) :-
    now(T), day(T, D0), Day is D0 + 1, N = Story.name, K = Story.kind,
    ( kind_traits(K, KT), member(entry(Po, Th0), KT) -> Th is float(Th0) ; Po = sponsor, Th = 4.0 ),
    ( has_closed(K) -> Policy = closed ; Policy = Po ),
    with_output_to(string(TJ), json_write_dict(current_output, Story.traits, [width(0)])),
    ( Ctx.type == arrival -> Off = 0.6, Str = 3.0 ; Off = 0.0, Str = 2.0 ),
    founding_effect(Ctx, Story, N, K, T, Day, Policy, Th, TJ, Off, Str, E).

has_closed(K) :- kind_traits(K, KT), memberchk(closed, KT).

founding_effect(Ctx, Story, N, K, T, Day, Policy, Th, TJ, Off, _,
                _{op: merge_node, label: 'Faction', name: N,
                  props: _{kind: K, scale: small, goal: Story.goal, description: Story.description, origin: Ctx.type,
                           traits: TJ, status: active, founded_at: T, founded_day: Day,
                           join_policy: Policy, join_standing: Th, offscreen: Off}}).
founding_effect(Ctx, _, N, _, _, _, _, _, _, _, _, E) :-
    nth0(I, Ctx.founders, P), ( I == 0 -> R = founder, L = 8.0 ; R = member, L = 7.0 ),
    (   E = _{op: link, a: P, a_label: 'Person', b: N, b_label: 'Faction', rel: 'MEMBER_OF', props: _{role: R, loyalty: L}}
    ;   Ctx.type == splinter, Parent = Ctx.parent,
        (   E = _{op: unlink, a: P, a_label: 'Person', b: Parent, b_label: 'Faction', rel: 'MEMBER_OF'}
        ;   E = _{op: add_rel_prop, a: P, a_label: 'Person', b: Parent, b_label: 'Faction', rel: 'STANDING',
                  prop: value, delta: -3.0, min: -10.0, max: 10.0}
        )
    ).
founding_effect(_, Story, N, _, _, _, _, _, _, _, _, E) :-
    member(R, Story.relations), get_dict(with, R, W0), atom_string(W, W0), group(W),
    get_dict(stance, R, S0), atom_string(S, S0), ( get_dict(reason, R, Why) -> true ; Why = "" ),
    stance_effects(N, W, S, Why, 0.0, Es), member(E, Es).
founding_effect(Ctx, _, N, _, _, _, _, _, _, _, Str,
                _{op: set_rel_prop, a: N, a_label: 'Faction', b: L, b_label: 'Location', rel: 'CONTROLS',
                  prop: strength, value: Str}) :-
    L = Ctx.location, L \== ''.
founding_effect(Ctx, Story, N, _, _, _, _, _, _, _, _, E) :-
    ( get_dict(parent, Ctx, Parent) -> Gs = [N, Parent] ; Gs = [N] ),
    chronicle(Ctx.type, Story.chronicle, Gs, Ctx.location, E).
founding_effect(Ctx, Story, N, _, _, _, _, _, _, _, _, _{op: remember, who: P, text: M, type: faction, location: Ctx.location}) :-
    member(P, Ctx.founders), format(atom(M), "You founded ~w: ~w", [N, Story.description]).

% ---------------------------------------------------------------------
% 招募、升格
% ---------------------------------------------------------------------

tick(recruit, E) :-
    group_due,
    findall(P-L, (location(P, L), living(P), \+ player(P)), PLs0), sort(PLs0, PLs),
    once(( member(P-L, PLs), controller(L, C), group(C), \+ direct(P, C),
           const(recruit_limit, R0), sum(C, recruit, R1), Lim is R0 + R1,
           ( aggregate_all(max(Lo), direct(P, _, _, Lo), Best) -> true ; Best = 0.0 ), Best =< Lim,
           \+ ( direct(P, G), stance(C, G, hostile) ) )),
    format(atom(T), "~w threw in their lot with ~w at ~w.", [P, C, L]),
    (   E = _{op: link, a: P, a_label: 'Person', b: C, b_label: 'Faction', rel: 'MEMBER_OF', props: _{role: member, loyalty: 6.0}}
    ;   chronicle(recruit, T, [C], L, E)
    ).

tick(rise, E) :-
    group_due, group(G), \+ has(G, enduring),
    aggregate_all(count, (direct(P, G), living(P)), Members),
    const(control_min, Min), aggregate_all(count, (strength(G, L, S), S >= Min, node('Location', L)), Held),
    Size is Members + Held, size_scale(Size, New), scale(G, Old),
    nth0(IN, [small, medium, large], New), nth0(IO, [small, medium, large], Old), IN > IO,
    format(atom(T), "~w has grown into a ~w power.", [G, New]),
    (   E = _{op: set_prop, entity: G, label: 'Faction', prop: scale, value: New}
    ;   chronicle(rise, T, [G], '', E)
    ).
