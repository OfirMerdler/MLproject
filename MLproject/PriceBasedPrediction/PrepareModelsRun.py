'''
Created on Jan 13, 2019

@author: mofir
'''

import torch
import numpy as np
import pandas as pd
from pandas import ExcelWriter

from PriceBasedPrediction.RunsParameters import NetworkModel
from PriceBasedPrediction.RunsParameters import LossFunctionMethod
from PriceBasedPrediction.RunsParameters import objectview

from PriceBasedPrediction.PrepareData import ConstructTestData,CreateDataset,GetDataVal
from utils.loggerinitializer import *

from Models import SimpleRNN, Dual_Lstm_Attn
from Models.SimpleRNN import RnnSimpleModel
from Models.Dual_Lstm_Attn import da_rnn
from Models import GeneralModelFn

from PriceBasedPrediction.RunsParameters import PredictionMethod,NormalizationMethod
from PriceBasedPrediction.PrepareData import ConfidenceAreas
from Statistics.CalculateStats import CalculateAllStatistics as CalcSt

from FeatureBuilder import stat_features

from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import GridSearchCV
from torch.utils.data import Dataset
from torch.utils.data import DataLoader

import pickle

import operator
import scipy.optimize as spo
####################################
import matplotlib
from matplotlib import pyplot as plt
####################################
import sklearn.metrics
from sklearn.model_selection import ParameterGrid
from sklearn.model_selection import KFold
from Statistics.CalculateStats import GetBuyVector
from Statistics.CalculateStats import GetProfit
from Statistics.CalculateStats import AvgGain
####################################
def GetEnsembleLearnersPred(learners_df,y_test, type = 'reg'):
    methods_list = ['bagging, stacking']

    def bagging(learners_df,type,weighted_learners):
        if (type=='reg'):
            weighted_learners
            pred_df = learners_df.mean(axis=1)
        elif (type=='clf'):
            for i in range(len(learners_df.index)):
                counts = {}
                for idx,value in enumerate(learners_df.iloc[i]):
                    if value in counts:
                        counts[value] += 1
                    else:
                        counts[value] = 1
            pred_df = max(counts.items(), key=operator.itemgetter(1))[0]
        else:
            print('error!! wrong type at GetEnsembleLearnersPred')
        return pred_df

    def constraint1_Sum(x):
        sum_abs = 1
        x_sum = x.sum()
        return (sum_abs - x_sum)

    def objective_SR_function(x, args,use_normalize = True):
        learners_df, rfr,sf = args
        if use_normalize:
            Position_vals = learners_df * x
        else:
            Position_vals = x

        Portfolio_val = Position_vals.sum(axis=1)
        dr = stat_features.compute_daily_returns(df=Portfolio_val,plot_graphs=False)
        dr = dr[1:]

        df_dr_minus_fr = dr - rfr

        SR_annual = df_dr_minus_fr.mean() / df_dr_minus_fr.std()
        SR_daily  =  np.sqrt(sf) * SR_annual
        return -SR_daily #we want maximum of SR

    def optimize_learners_weights(learners_df):
        num_of_symb = learners_df.shape[1]
        InitialGuess = np.full(1,num_of_symb,1/num_of_symb) #equal distribution
        args_list_for_optimizer = [learners_df,0,252]
        constraint1 = {'type': 'eq', 'fun': constraint1_Sum}
        range = (0,1)
        bound = 0.9
        range_list = np.full(1,num_of_symb,bound)
        sol_SLSQP = spo.minimize(fun=objective_SR_function,args=args_list_for_optimizer,x0 = InitialGuess, bounds = range_list,method='SLSQP',constraints = constraint1)
        sol_BFGS  = spo.minimize(fun=objective_SR_function,args=args_list_for_optimizer,x0 = InitialGuess, bounds = range_list,method='BFGS',constraints = constraint1)

        print(sol_SLSQP.x)
        print(sol_BFGS.x)
        weighted_learners = sol_SLSQP.x
        pred_df = bagging(learners_df,type = 'reg', weighted_learners = weighted_learners)

        return pred_df

    pred_df_simple = bagging(learners_df,type)
    pred_df_opt    = optimize_learners_weights(learners_df,type)

    return pred_df


def TuneSimpleRNN(model_params,clf_type,X, y,file_path, cv=2):

    kf = KFold(n_splits=cv)

    grid = ParameterGrid(model_params.__dict__)
    best_error = np.inf
    best_error_slope = np.inf
    best_profit = 0

    error_slope_list = []
    error_list = []
    profit_list = []
    params_list = []

  #X_df = pd.DataFrame(X)
  #y_df = pd.DataFrame(y)
    total_params = len(grid)
    p = 0

    for params in grid:
        print("parameters iter in RNN GridSearch is: " + str(p) + " out of: " + str(total_params))
        print(params)
        p = p+1
        profit = 0
        error = 0
        error_slope = 0
        params = objectview(params)
        for train_index, test_index in kf.split(X):
            #X_train, X_test = X_df.iloc[train_index], X_df.iloc[test_index]
            #y_train, y_test = y_df.iloc[train_index], y_df.iloc[test_index]
            X = pd.DataFrame(X)
            X_train, X_test = X.iloc[train_index], X.iloc[test_index] #np.take(X, train_index,out=new_shape), np.take(X, test_index)
            y_train, y_test = np.take(y, train_index), np.take(y, test_index)
            X_train, X_test = X_train.values,X_test.values
            train_dataset = CreateDataset(X_train,y_train)
            train_loader = DataLoader(dataset=train_dataset, batch_size=params.batch_size, shuffle=False, num_workers=1)

            input_size  = X_train.shape[1]
            output_size = y_train.ndim

            SimpleRNN.Train(input_size, params.hidden_layer_size, output_size,train_loader,file_path,params.learning_rate,params.num_epochs)

            test_dataset = CreateDataset(X_test,y_test)
            test_loader = DataLoader(dataset=test_dataset, batch_size=params.batch_size, shuffle=False, num_workers=0)
            loss_fn = GeneralModelFn.loss_fn
            metrics = GeneralModelFn.metrics
            model = RnnSimpleModel(input_size = input_size, rnn_hidden_size = params.hidden_layer_size, output_size = output_size)

            y_pred, evaluation_summary = SimpleRNN.Predict(model,loss_fn,test_loader, metrics, cuda = False)
            #print(evaluation_summary)

            if (clf_type=='cat'):
                y_error = np.equal(y_pred,y_test)
                error = error + y_error.sum()/len(y_test)
            else:
                buy_vector  = GetBuyVector(y_pred)
                curr_profit = AvgGain(y_test,buy_vector)

                y_previous_true = y_test[0:-1]
                y_current_true  = y_test[1:]
                y_previous_pred = y_pred[0:-1]
                y_current_pred  = y_pred[1:]

                y_true_slope = y_current_true > y_previous_true
                y_pred_slope = y_current_pred > y_previous_pred

                y_pred_direction_false = y_true_slope != y_pred_slope

                error_slope = error_slope + y_pred_direction_false.sum()
                error = error + sklearn.metrics.mean_squared_error(y_test,y_pred)
                profit = profit + curr_profit
                print("curr profit is: " + str(curr_profit))
        if error < best_error:
            best_params = params
            best_error = error

        if error_slope < best_error_slope:
            best_params_slope = params
            best_error_slope = error_slope

        if profit > best_profit:
            best_params_profit = params
            best_profit = profit

        error_slope_list.append(error_slope)
        error_list.append(error)
        profit_list.append(profit)
        params_list.append(params)

    df_err_summary = pd.DataFrame()
    df_err_summary['parameters'] = params_list
    df_err_summary['error'] = error_list
    df_err_summary['slope_error'] = error_slope_list
    df_err_summary['profit'] = profit_list

    return best_params,best_params_slope,best_params_profit,df_err_summary

def TrainSimpleRNN(x_train,y_train,model_params,clf_type,file_path,general_params):

    if (general_params.tune_needed == True):
        logging.info("starts tuning parameters RNN.....")
        best_params,best_params_slope,best_params_profit,df_err_summary = TuneSimpleRNN(model_params,clf_type,x_train, y_train,file_path, cv=2)
        my_best_params = best_params_profit

        writer = ExcelWriter(r"rnn_tune_results.xlsx")
        df_err_summary.to_excel(writer, startrow=0,startcol=0)
        writer.save()
        writer.close()

    else:
        my_best_params = model_params

    train_dataset = CreateDataset(x_train,y_train)
    train_loader = DataLoader(dataset=train_dataset, batch_size=my_best_params.batch_size, shuffle=False, num_workers=1)
    input_size  = x_train.shape[1]
    output_size = y_train.shape[1]
    SimpleRNN.Train(input_size, my_best_params.hidden_layer_size, output_size,train_loader,file_path,my_best_params.learning_rate,my_best_params.num_epochs)
    return my_best_params


def PredictSimpleRNN(x_test,y_test,model_params,use_cuda,file_path):
    print("hello from PredictSimpleRnn")
    test_dataset = CreateDataset(x_test,y_test)
    test_loader = DataLoader(dataset=test_dataset, batch_size=model_params.batch_size, shuffle=False, num_workers=0)

    input_size  = x_test.shape[1]
    hidden_size = model_params.hidden_layer_size
    output_size = y_test.shape[1]

    model = RnnSimpleModel(input_size = input_size, rnn_hidden_size = hidden_size, output_size = output_size)

    try:
        model.load_state_dict(torch.load(file_path))
    except:
        print("error!! didn't find trained model")

    loss_fn = GeneralModelFn.loss_fn
    metrics = GeneralModelFn.metrics

    labels_prediction_total, evaluation_summary = SimpleRNN.Predict(model,loss_fn,test_loader, metrics, cuda = use_cuda)

    fig = plt.figure()
    ax1 = fig.add_subplot(2,1,1)
    ax1.plot(evaluation_summary)
    ax1.set_title('losses for test RNN ')
    fig.savefig('losses for test RNN' + '.png')

    return (labels_prediction_total)

def TrainPredictDualLSTM(x_train,y_history,y_train,x_test,y_test,model_params,file_path,general_params):
    logging.info("hello from TrainPredictDualLSTM")
    print("hello from TrainPredictDualLSTM")

    if (general_params.tune_needed == True):
        logging.info("starts tuning parameters.....")
        DualLSTM_clf_tune = Dual_Lstm_Attn.da_rnn(input_size = x_train.shape[1])

        logging.debug("x_train shape: ")
        logging.debug(x_train.shape)
        my_best_params_mse_error,my_best_params_slope, my_best_params_profit,df_err_summary = Dual_Lstm_Attn.tune(
                                                                                    DualLSTM_clf_tune,
                                                                                    x_train,y_history,
                                                                                    y_train,input_size = 1,
                                                                                    arguments=model_params.__dict__,
                                                                                    curr_stock = general_params.stock_name,
                                                                                    cv=2)
        writer = ExcelWriter(r"LSTM_tune_results.xlsx")
        df_err_summary.to_excel(writer, startrow=0,startcol=0)
        writer.save()
        writer.close()

        my_best_params = my_best_params_profit
        logging.info("summary profit for all parameters")
        logging.info (df_err_summary['profit'])

        logging.info("summary mse error for all parameters")
        logging.info (df_err_summary['error'])

        logging.info("summary momentum for all parameters")
        logging.info (df_err_summary['slope_error'])

        logging.info ("best parameters for profit")
        logging.info (my_best_params_profit)
        logging.info ("best parameters for mse error")
        logging.info (my_best_params)
        logging.info ("best parameters for slope")
        logging.info (my_best_params_slope)


        with open('lstmDual_bestParams.pickle', 'wb') as output_file:
            pickle.dump(my_best_params,output_file)

    elif (general_params.load_best_params == True):
        with open('lstmDual_bestParams.pickle', 'rb') as f:
            my_best_params = pickle.load(f)
    else:
        my_best_params = model_params.__dict__


    logging.info("starts training: Dual_Lstm_Attn.da_rnn")
    input_size = 1

    clf_DualLSTM = Dual_Lstm_Attn.da_rnn(input_size = input_size)
    clf_DualLSTM.__init__(input_size,**my_best_params)

    #pnl_loss =  model_params.loss_method == LossFunctionMethod.pnl

    logging.debug(x_train.shape)
    logging.debug(y_train.shape)

    x_train   = pd.DataFrame(x_train)
    y_history = pd.DataFrame(y_history)
    if (general_params.only_train==False):
        plot_results = True
        x_test    = pd.DataFrame(x_test)
    else:
        plot_results = False

    if (general_params.train_needed == True):
        clf_DualLSTM.Train(x_train,y_history,y_train, x_test,y_test,plot_results=plot_results,curr_stock = general_params.stock_name)
        if (general_params.SaveTrainedModel==True):
                torch.save(clf_DualLSTM.state_dict(), file_path)
    else:
        try:
            clf_DualLSTM.load_state_dict(torch.load(file_path))
        except:
            print("error!! didn't find trained model")

    if (general_params.only_train==False):
        logging.info("hello from PredictDualLSTM")
        y_pred = clf_DualLSTM.Predict(x_test,y_test)
        logging.debug(y_test.shape)
        logging.debug(y_pred.shape)
    else:
        y_pred = None

    return y_pred, my_best_params

def PlotRandForest(x_train,y_train,y_train_raw):
    print(y_train_raw.shape)
    print(y_train.shape)
    set_high_rise = np.zeros(y_train_raw.shape)
    set_high_rise [ y_train == ConfidenceAreas.rise_high ] = 1
    high_predict  = set_high_rise * y_train_raw
    high_predict [ high_predict==0 ] = np.nan
    set_low_high_rise = np.zeros(y_train_raw.shape)
    set_low_high_rise [ y_train != ConfidenceAreas.rise_high ] = 1
    low_high_predict  = set_low_high_rise * y_train_raw
    low_high_predict [ low_high_predict==0 ] = np.nan

    plt.figure()
    plt.plot(y_train_raw,'r--',color='blue', label = "stock timeline")
    plt.plot(high_predict,'o',color='green', label = "y train buy")
    plt.plot(low_high_predict,'o',color='yellow', label = "y train buy")
    plt.ylabel('Price and decision (green)')
    plt.xlabel('time line')
    plt.title('RFC - train data vs. strong buy advice')
    plt.show()

def TrainPredictRandForest(x_train,y_train,x_test,y_test,model_params,file_path):
    from sklearn import utils
    from sklearn import preprocessing

    print("hello from TrainPredictRandForest")
    plot_train_x_y = False
    if (plot_train_x_y == True):
            y_train_raw =y_train #TODO- need to be the close values
            PlotRandForest(x_train,y_train,y_train_raw)

    #TODO - maybe change to WRF

    #WRF_classifier = WRF()
    #arguments = {'n_trees': [8,5],'max_depth':[5], 'n_features':[None], 'weight_type':["sub"]}
    #my_best_params = tune(WRF_classifier,x_train,y_train,arguments, cv=5)
    #my_best_params = model_params
    #WRF_best = WRF(n_trees = my_best_params['n_trees'], max_depth=my_best_params['max_depth'], n_features=my_best_params['n_features'], weight_type=my_best_params['weight_type'], type="cat")
    #WRF_best = WRF(**gcv.best_params_)

    RFC_classifier = RandomForestClassifier() if model_params.prediction_method==PredictionMethod.MultiClass else RandomForestRegressor()

    if (model_params.tune_branch_needed == True):
        if (model_params.prediction_method==PredictionMethod.MultiClass):
            arguments = {'n_estimators': [100],'max_depth':[6,10,18], 'max_features':[None], 'criterion':["entropy"]}#gini
        else:
            arguments = {'n_trees': [80],'max_depth':[6,10,18], 'n_features':[None,0.5], 'weight_type':["sub","div"]}

        GS = GridSearchCV(estimator=RFC_classifier,param_grid=arguments, cv=5)
        GS.fit(x_train,y_train)


        sklearn_best_params =  GS.best_params_
        print(sklearn_best_params)
        gs_cv_results = GS.cv_results_
        print(gs_cv_results['params'])
        print(gs_cv_results['mean_test_score'])
        print(gs_cv_results['std_test_score'])
    else:
        if (model_params.prediction_method==PredictionMethod.MultiClass):
            sklearn_best_params = {'n_estimators': 100,'max_depth':15, 'max_features':None, 'criterion':"entropy"}
        else:
            sklearn_best_params = {'n_trees': [80],'max_depth':[15], 'n_features':[None], 'weight_type':["sub"]}

    RFC_best = RFC_classifier.set_params(**sklearn_best_params)

    if (model_params.load_trained_model == True):
        try:
            RFC_best.load_state_dict(torch.load(file_path))
        except:
            print("error!! didn't find trained model")
    else:
        RFC_best.fit(x_train,y_train)


    print("starts predicting RFC best...")
    y_pred_confidence = RFC_best.predict_proba(x_test)
    #print(y_pred_confidence)

    plt_rfc_res = False
    if plt_rfc_res == True:
        set_high_rise_a = np.zeros(y_test.shape)
        set_high_rise_b = np.zeros(y_test.shape)

        set_high_rise_a [y_pred_confidence[:,0] > 0.5] = 1
        set_high_rise_b [y_pred_confidence[:,1] > 0.5] = 1

        set_high_rise = np.logical_or(set_high_rise_a,set_high_rise_b)

        high_predict  = set_high_rise * y_test
        #we don't want to plot zeroes, only on the stock value
        high_predict [ high_predict==0 ] = np.nan
        plt.figure()
        plt.plot(high_predict,'o',color='green', label = "buy decision successful")
        plt.plot(y_test,color='blue', label = "stock timeline")
        plt.ylabel('Price and decision (green)')
        plt.xlabel('time line')
        plt.title('RFC - test data vs. strong buy advice')
        plt.show(block=False)

    fig = plt.figure()
    y_all = y_test #np.concatenate((y_train, y_test)) # y_test
    ax = fig.add_subplot(2,1,1)
    ax.plot(y_all, label = "True")
    ax.plot(y_pred_confidence, label = 'Predicted - Test')
    ax.set_title("RFC prediction : test vs. pred")
    fig.savefig('RFC prediction test vs train' + '.png')

    y_pred = y_pred_confidence
    return y_pred,RFC_best



########################################3

#class Statistics_Func():

def GetPredictionSummary(curr_config,general_config,curr_model,target_type,real_close_value,real_value,predictad_value):

    if (target_type == 'reg'):
        real_value = real_value.reshape(1,-1)[0]
        real_values_adj = real_value if curr_config.normalization_method!=NormalizationMethod.Naive else 300*real_value
        pred_values_adj = predictad_value if curr_config.normalization_method!=NormalizationMethod.Naive else 300*predictad_value
        real_values_adj = real_values_adj
        pred_values_adj = pred_values_adj
    else:
        real_values_adj = real_value
        pred_values_adj = predictad_value

    prediction_stats_df = CalcSt(real_close_value,real_values_adj,pred_values_adj,target_type,plot_buy_decisions = True).loc[0]
    #print(prediction_stats_df)

    buy_decision_summary = pd.DataFrame(columns=['close_values','predicted_price','buy_decision','real_buy_decision'])

    pred_buy_decision = GetBuyVector(predictad_value)
    real_buy_decision = GetBuyVector(real_value)
    print(real_values_adj[0:5])
    print(pred_values_adj[0:5])
    print(real_buy_decision[0:5])
    print(pred_buy_decision[0:5])

    buy_decision_summary['close_values']      = real_values_adj[:-1]
    buy_decision_summary['predicted_price']   = pred_values_adj[:-1]
    buy_decision_summary['real_buy_decision'] = real_buy_decision
    buy_decision_summary['buy_decision']      = pred_buy_decision

    buy_decision_summary_df = pd.DataFrame(columns=[general_config.stock_name])
    buy_decision_summary_df = buy_decision_summary_df.append(buy_decision_summary, ignore_index=True)
    #buy_decision_summary['good_decision']     = pred_buy_decision==real_buy_decision

    def color_bad_decision(val):
        #copy df to new - original data are not changed
        df = val.copy()
        #select all values to default value - red color
        df[['buy_decision']] = 'red'
        #overwrite values green color
        df.loc[df['good_decision'] == True, 'buy_decision'] = 'green'
        return df

    #buy_decision_summary = buy_decision_summary.style.apply(color_bad_decision, axis=None)
    #print(buy_decision_summary)

    def GetBalanceOverTime(real_close_values,buy_vector,curr_stock,curr_model):
        init_balance = 1

        today_real_value    = real_close_values[0:-1]
        tommorow_real_value = real_close_values[1:]
        real_buy_vector     = GetBuyVector(real_close_values)

        curr_balance = init_balance
        opt_balance = init_balance

        balance_per_day = []
        opt_balance_per_day = []

        for ind,val in enumerate(today_real_value):
            curr_balance = curr_balance * (tommorow_real_value[ind]/today_real_value[ind]) * buy_vector[ind] + curr_balance * (1 - buy_vector[ind])
            balance_per_day.append(curr_balance)

            opt_balance = opt_balance * (tommorow_real_value[ind]/today_real_value[ind]) * real_buy_vector[ind] + opt_balance * (1 - real_buy_vector[ind])
            opt_balance_per_day.append(opt_balance)

        fig = plt.figure()
        ax1 = fig.add_subplot(2,1,1)
        ax1.plot(balance_per_day)
        ax1.plot(opt_balance_per_day,color='g' , label = 'Maximum gain graph')
        ax1.set_title(curr_model + ' balance over time for ' + curr_stock)
        ax1.set_xlabel('time-line')
        ax1.set_ylabel('balance')

        ax2 = fig.add_subplot(2,1,2)
        ax2.plot(today_real_value,'--',color='y' , label = 'real graph')
        ax2.set_xlabel('time-line')
        ax2.set_ylabel('stock value')
        ax2.set_title('real graph for ' +   curr_stock)
        fig.savefig(curr_model + ' balance over time for ' + curr_stock + '.png')

    GetBalanceOverTime(real_close_value,pred_buy_decision,general_config.stock_name,curr_model)

    return prediction_stats_df,buy_decision_summary_df

def RunNetworkArch(df,df_branch, model_params):
    test_train_split = False if model_params.only_train else True #model_params.network_model!=NetworkModel.DualLstmAttn

    DualLSTM_file_path      = 'my_DualLSTM_model.model'
    simple_rnn_file_path    = 'my_simple_rnn_model.model'
    random_forest_file_path = 'my_RFC_model.model'

    best_config = model_params.LSTM_HyperParameters

    logging.info("preparing data.....")

    if model_params.network_model==NetworkModel.simpleRNN:
        RNN_config = model_params.RNN_HyperParameters['ModelParams']
        data_params = model_params.RNN_HyperParameters['GeneralParams']
        rnn_clf_type = 'cat' if (data_params.prediction_method==PredictionMethod.MultiClass) else 'reg'

        Data  = ConstructTestData(df, data_params,test_train_split,model_params.train_data_precentage)
        x_train,y_history,y_train,x_test,y_test = GetDataVal(Data,only_train=model_params.only_train)
        real_close_value = df['close'].tail(x_test.shape[0]).tolist()

        RNN_config_best_params = TrainSimpleRNN(x_train,y_train,RNN_config,rnn_clf_type,simple_rnn_file_path,model_params)
        if (model_params.only_train==False):
            y_pred = PredictSimpleRNN(x_test,y_test,RNN_config_best_params,model_params.use_cuda,simple_rnn_file_path)
        best_config = RNN_config

        prediction_stats_df,buy_decision_summary_df = \
        GetPredictionSummary(data_params,model_params,'RNN',rnn_clf_type,real_close_value,y_test,y_pred)

    elif model_params.network_model==NetworkModel.RandomForest:
        rf_clf_type = 'cat' if (data_params.prediction_method==PredictionMethod.MultiClass) else 'reg'
        if (model_params.prediction_method==PredictionMethod.MultiClass):
            arguments = {'n_estimators': [100],'max_depth':[6,10,18], 'max_features':[None], 'criterion':["entropy"]}#gini
        else:
            arguments = {'n_trees': [80],'max_depth':[6,10,18], 'n_features':[None,0.5], 'weight_type':["sub","div"]}

        RFR_config = model_params.RFR_HyperParameters['ModelParams']
        data_params = model_params.RFR_HyperParameters['GeneralParams']
        Data  = ConstructTestData(df, data_params,test_train_split,model_params.train_data_precentage)
        x_train,y_history,y_train,x_test,y_test = GetDataVal(Data,only_train=model_params.only_train)
        real_close_value = df['close'].tail(x_test.shape[0]).tolist()
        y_pred, best_config = TrainPredictRandForest(x_train,y_train,x_test,y_test,RFR_config,random_forest_file_path)
        print(y_pred)
        prediction_stats_df,buy_decision_summary_df = \
        GetPredictionSummary(data_params,model_params,'RFR',rf_clf_type,real_close_value,y_test,y_pred)

    elif model_params.network_model==NetworkModel.DualLstmAttn:
        DualLstm_config = model_params.LSTM_HyperParameters['ModelParams']
        data_params = model_params.LSTM_HyperParameters['GeneralParams']
        lstm_clf_type = 'cat' if (data_params.prediction_method==PredictionMethod.MultiClass) else 'reg'
        Data  = ConstructTestData(df, data_params,test_train_split,model_params.train_data_precentage)
        x_train,y_history,y_train,x_test,y_test = GetDataVal(Data, model_params.only_train)
        real_close_value = df['close'].tail(x_test.shape[0]).tolist()
        y_pred, best_config = TrainPredictDualLSTM(x_train,y_history,y_train,x_test,y_test,DualLstm_config,DualLSTM_file_path,model_params)

        prediction_stats_df,buy_decision_summary_df = \
        GetPredictionSummary(data_params,model_params,'LSTM',lstm_clf_type,real_close_value,y_test,y_pred)

    elif model_params.network_model==NetworkModel.EnsambleLearners:
        #1. Dual Lstm
        print("ensemble learners starts...")
        print("DualLSTM...")
        y_pred_lstm, best_config = TrainPredictDualLSTM(x_train,y_history,y_train,x_test,y_test,model_params,DualLSTM_file_path,model_params)
        lstm_prediction_stats_df,lstm_buy_decision_summary_df = \
        GetPredictionSummary(DualLstm_config,model_params,'LSTM',lstm_clf_type,y_test,y_pred)
        #2. simple Rnn
        print("RNN...")
        TrainSimpleRNN(x_train,y_history,y_train,x_test,y_test,model_params,DualLSTM_file_path)
        y_pred_rnn  = PredictSimpleRNN(x_test,y_test,model_params,simple_rnn_file_path)
        #2. RFC/WRC
        print("RFR...")
        y_pred_rfc,  = TrainPredictRandForest(x_train,y_train,x_test,y_test,model_params,random_forest_file_path)
        #4. linear regressor

        #5.
        learners_df = pd.DataFrame(columns=['lstm','rnn','rfc','linear_reg'])
        y_pred = GetEnsembleLearnersPred(learners_df,y_test,type = 'reg')

        plot_df = learners_df.copy()
        plot_df['ensemble_pred'] = y_pred
        plot_df['real_data']     = y_test

        from Statistics.CalculateStats import AvgGain
        from Statistics.CalculateStats import GetBuyVector

        gain_df_all_methods = pd.DataFrame(columns=plot_df.columns.tolist())
        for column in plot_df.columns:
            gain_df_all_methods[column] = AvgGain(plot_df[column],GetBuyVector(plot_df[column]))

        print(gain_df_all_methods)

        ax = plot_df.plot(title='real close prices & predictions as a function of time')
        ax.set_xlabel=("Date")
        ax.set_xlabel=("Price")

        buy_decision_summary_df = pd.DataFrame(columns=[general_config.stock_name])
        buy_decision_summary_df = buy_decision_summary_df.append(lstm_buy_decision_summary_df, ignore_index=True)

        prediction_stats_df = lstm_prediction_stats_df
    else:
        print("need to add default network")

    AddRFConfidenceLevel = model_params.AddRFConfidenceLevel == True
    if (AddRFConfidenceLevel):
        buy_vector_confidence = TrainPredictRandForest(df_branch,model_params)
    else:
        buy_vector_confidence = None

    if (model_params.only_train == False):
        logging.debug("y_pred shape is: " + str(len(y_pred)))
        logging.debug(y_pred[0:5])

        i=0
        #while i < min(10,len(y_pred)):
        #    logging.info("y_pred is:" + str(y_pred[i]))
        #    logging.info("y_true is:" + str(y_test[i]))
        #    i=i+1

        plt.figure()
        #TODO - the flatten is not good, need to flatten it in a different way
        plt.plot(y_pred.flatten(),'r',Data['y_ho_data'].flatten(),'b')
        plt.ylabel('Price - red predication, blue real')
        plt.xlabel('time line')
        plt.show(block=False)
    #TODO - maybe worth to run the network on another stock to see if we can use same training for various stocks

    prediction_summary = {
        'stats': prediction_stats_df,
        'buy_summary': buy_decision_summary_df
        }

    return y_test.flatten(),y_pred.flatten(),buy_vector_confidence,best_config,prediction_summary